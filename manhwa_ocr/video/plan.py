"""The shot list: what is on screen when, laid out and animated how.

video/chapter_N.plan.json is the single source of truth for a render. It is
plain JSON so the user or an agent can fix a shot by editing a field; the
renderer only re-draws shots whose spec changed.

Each shot:
    {"id": "S012", "start": 31.2, "end": 34.0,
     "layout": "single" | "pair" | "inset" | "wide",
     "background": "blur" | "black" | "white",
     "bg_panel": "P004-01",               # optional; blurred background source
     "cards": [{"panel": "P004-02", "crop": [x0, y0, x1, y1],   # fractions
                "enter": "fade_in", "at": 0.0, "motion": "push_in"}],
     "exit": "cut" | "fade_out",
     "effects": [{"type": "shake", "t": 32.0}],
     "sentence": "..."}

Pacing follows manhwa_recap_style_spec.md: one sentence is about one shot
(about 2.7s on average), hard cuts, continuous Ken Burns motion across the whole shot (P3: no frozen holds \u2014 `hold` is reserved for hand-set payoff beats).
"""

from __future__ import annotations

from collections import OrderedDict
import json
import math
import random
import zlib
from pathlib import Path
from typing import Any, Callable, Dict, List, Tuple

from PIL import Image

from ..util import UserError, atomic_write_text, read_agent_json, require_chapter
from . import sync as sync_mod
from .crops import _panel_maps, head_of, is_mask_risk, is_system_window, tone
from .cropcache import crop_key, load_crop, store_crop
from .verify import find_pair_safe_shape, pick_card
from .editor import (PAN_DIRECTIONS, SFX_ALL, _inset_crop, build_chapter_cards, extract_callout, pacing_qa,
                     plan_sfx, resolve_beats, resolve_music, snap_cuts_to_beats, split_long_shots)
from .editor import MOTION_WEIGHTS as P3_MOTION_WEIGHTS
from .captions import build_captions, build_captions_from_sentences, validate_captions
from .cinematic import layout_for as _cinematic_layout_for
from .frames import (BACKGROUNDS, EFFECTS, ENTRANCES, EXITS, GLITCH_MIN_GAP, LAYOUTS, MOTIONS,
                     TRANSITION_WEIGHTS, TRANSITIONS, Format, format_from, shapes_for, transition_enter)
from .media import video_dir

PLAN_VERSION = 2
MIN_SYNC_SHOT = 1.0      # shorter shots merge into the previous one (avoids flicker)
MAX_SYNC_SHOT = 5.5      # max duration when merging sentences sharing the same panel
MAX_PAIR_SHOT = 6.5      # max total duration allowed for a 2-panel pair shot
PUNCH_GAP = 4.0
EFFECT_GRACE = 0.35
MIN_HOLD = 3.0           # seconds: no picture is held on screen for less than this
MIN_SHOT = 3.0           # seconds: no SHOT is ever shorter than this (except the
                         # renderer's 0.5s black tail). The sync gate enforces it;
                         # hold_groups() uses the caller's min_hold as its floor
                         # (chapter default 3.0 = MIN_SHOT; the intro trailer
                         # deliberately uses 2.0 and is never sync-audited).
PAD_LIMIT = 1.5          # a card's pad_y (blurred padding above and below, in panel heights) is at most this

# P3 continuous motion (editor.py): the planner never emits `hold`. A reviewer
# may still hand-set motion="hold" for a deliberate 1-2s post-payoff beat;
# frames.py keeps the old ease-out-then-hold for exactly that case.
ENTRANCE_WEIGHTS = [("cut", 90), ("fade_in", 10)]
MOTION_WEIGHTS = P3_MOTION_WEIGHTS


def plan_path(root: Path, chapter: str) -> Path:
    return video_dir(root) / f"chapter_{require_chapter(chapter)}.plan.json"


def _rng(chapter: str, i: int) -> random.Random:
    return random.Random(zlib.crc32(f"{chapter}:{i}".encode()))


def _pick(rng: random.Random, weights, avoid: str | None = None) -> str:
    opts = [(k, w) for k, w in weights if k != avoid or k == "cut"]
    total = sum(w for _, w in opts)
    x = rng.uniform(0, total)
    for k, w in opts:
        x -= w
        if x <= 0:
            return k
    return opts[-1][0]


# ------------------------------------------------------------------ timing units


def even_units(panels: List[Dict[str, Any]], duration: float, vcfg: Dict[str, Any]) -> List[Dict[str, Any]]:
    """No tags: every drawn panel, in story order, shares the narration's length.
    Longer and text-heavier panels get a little more time. Nothing is dropped:
    text-only panels (captions, blank pages, credits) are the only exclusion,
    and hold_groups() packs them into shots under the cut budget."""
    if not panels:
        raise UserError("No panels to put in the video.")
    art = [p for p in panels if not p.get("text_only")] or list(panels)
    weights = [1.0 + min(len(p.get("text") or ""), 300) / 150 + (0.4 if p.get("tall") else 0) for p in art]
    total = sum(weights) or 1.0
    units, t = [], 0.0
    for p, w in zip(art, weights):
        d = duration * w / total
        units.append({"start": t, "end": t + d, "panels": [p["id"]], "cues": [], "emphasis": [],
                      "sentence": None, "sentence_starts": [t]})
        t += d
    units[-1]["end"] = duration
    return units


def _nearest_art(panels: List[Dict[str, Any]], index: int, avoid: set, reach: int = 3) -> str | None:
    """The drawn panel nearest to panels[index] (later ones first, the story moves
    forward) that isn't text-only and isn't in `avoid`."""
    for d in range(1, reach + 1):
        for j in (index + d, index - d):
            if 0 <= j < len(panels) and not panels[j].get("text_only") and panels[j]["id"] not in avoid:
                return panels[j]["id"]
    return None


MIN_PIECE = 1.8   # a panel inside a multi-panel sentence stays at least this long


def synced_units(panels: List[Dict[str, Any]], sentences: List[Dict[str, Any]], duration: float,
                 vcfg: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Panel by panel: every sentence shows the panels it is tagged with, in order.

    - A sentence tagged with several panels shows them one after another,
      splitting its time between them (each at least MIN_PIECE seconds).
    - A tag on a speech-bubble, lettering or credits panel shows the nearest
      drawn panel that isn't already on screen nearby; otherwise the current
      picture stays.
    - The same panel is never shown twice in a row: consecutive sentences about
      one panel are one shot. Nothing is added that the script didn't tag.
    """
    known = {p["id"] for p in panels}
    index = {p["id"]: i for i, p in enumerate(panels)}
    lead = float(vcfg.get("lead", 0.15))
    first_art = next((p["id"] for p in panels if not p.get("text_only")), panels[0]["id"])
    current: List[str] = [first_art]
    units: List[Dict[str, Any]] = []

    def add(unit: Dict[str, Any]) -> None:
        if units and (unit["end"] - unit["start"] < MIN_SYNC_SHOT or unit["panels"] == units[-1]["panels"]):
            u = units[-1]
            u["end"] = unit["end"]
            u["cues"] += unit["cues"]
            u["emphasis"] += unit["emphasis"]
            if unit["sentence"] and unit["sentence"] != u["sentence"]:
                u["sentence"] = " ".join(x for x in (u["sentence"], unit["sentence"]) if x)
            u["sentence_starts"] += unit["sentence_starts"]
            return
        units.append(unit)

    for i, s in enumerate(sentences):
        start = 0.0 if i == 0 else max(0.0, float(s["start"]) - lead)
        end = duration if i + 1 >= len(sentences) else max(start, float(sentences[i + 1]["start"]) - lead)
        ids: List[str] = []
        for p in (x for x in s.get("panels") or [] if x in known):
            if panels[index[p]].get("text_only"):
                if panels[index[p]].get("credits"):
                    continue                      # scanlator pages are never part of the story
                p = _nearest_art(panels, index[p], set(ids) | set(current)) or ""
            if p and p not in ids:
                ids.append(p)
        ids = ids or current
        # Each tagged panel gets its own stretch of the sentence, as long as
        # every stretch is long enough to take in.
        fits = max(1, min(len(ids), int((end - start) // MIN_PIECE)))
        ids = ids[:fits]
        step = (end - start) / len(ids)
        for k, pid in enumerate(ids):
            a = start + k * step
            b = end if k == len(ids) - 1 else a + step
            add({"start": a, "end": b, "panels": [pid],
                 "cues": list(s.get("cues") or []) if k == 0 else [],
                 "emphasis": [e for e in (s.get("emphasis") or []) if a <= e < b],
                 "sentence": s.get("text"), "sentence_starts": [float(s["start"]) if k == 0 else a]})
        current = ids[-1:]
    if not units:
        return even_units(panels, duration, vcfg)
    if len(units) > 1 and units[0]["end"] - units[0]["start"] < MIN_SYNC_SHOT:
        # Nothing comes before the first unit to merge it into, so the next one
        # absorbs it (e.g. a one-word opening line, or a misheard first sentence).
        first, nxt = units.pop(0), units[0]
        nxt["cues"] = first["cues"] + nxt["cues"]
        nxt["emphasis"] = first["emphasis"] + nxt["emphasis"]
        nxt["sentence"] = " ".join(x for x in (first["sentence"], nxt["sentence"]) if x)
        nxt["sentence_starts"] = first["sentence_starts"] + nxt["sentence_starts"]
    units[0]["start"] = 0.0
    units[-1]["end"] = duration
    return units


def units_from_alignment(segments: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Translate chapter_N.alignment.json's segments into the same unit shape
    synced_units()/even_units() produce, so everything downstream (pairing,
    layouts, crops, entrances) is unchanged.

    This is a pure translation, nothing else: the panel for each unit is
    whatever the alignment already decided (`selected_panel` on SHOW,
    whatever panel is already current on HOLD_PREVIOUS). No panel is chosen,
    scored or guessed at here - that already happened in align_engine.py.
    Consecutive segments that resolve to the same panel are merged into one
    shot, exactly as synced_units() already does for repeated tags.
    """
    units: List[Dict[str, Any]] = []

    def add(unit: Dict[str, Any]) -> None:
        # Only the same panel in a row is merged. A short unit of a different
        # panel stays its own unit: hold_groups() gives it time, so no panel is
        # ever dropped here.
        if units and unit["panels"] == units[-1]["panels"]:
            u = units[-1]
            u["end"] = unit["end"]
            if unit["sentence"] and unit["sentence"] != u["sentence"]:
                u["sentence"] = " ".join(x for x in (u["sentence"], unit["sentence"]) if x)
            u["sentence_starts"] += unit["sentence_starts"]
            return
        units.append(unit)

    current: str | None = None
    for seg in segments:
        if seg.get("display_action") == "SHOW" and seg.get("selected_panel"):
            # "sequence" = every panel this sentence shows, in story order
            # (the selected one plus any impactful filler place_impactful()
            # chose for a long sentence). A hand-edited selected_panel
            # replaces the panel the alignment chose; the fillers placed in
            # this sentence are kept, so no panel is dropped by an edit.
            sel = seg["selected_panel"]
            old = seg.get("selected_by_align")
            extras = [p for p in (seg.get("sequence") or []) if p not in (sel, old)]
            seq = [sel, *extras] if sel != old and extras else (seg.get("sequence") or [sel])
            if sel not in seq:
                seq = [sel, *seq]
            current = seq[-1]     # the panel left on screen after this sentence
        else:
            seq = [current] if current is not None else []
        if not seq:
            continue  # nothing has ever been shown yet; nothing to hold either
        start, end = float(seg["start"]), float(seg["end"])
        # Split on the shares place_impactful() recorded (tagged ~65% +
        # filler ~35% for a long sentence), NOT evenly: an even split
        # would give the filler half the window and break tagged coverage.
        # Segments without shares (intro, hand-built) split evenly.
        shares = list(seg.get("sequence_shares") or [])
        if len(shares) != len(seq) or abs(sum(shares) - 1.0) > 1e-6:
            shares = [1.0 / len(seq)] * len(seq)
        cursor_t = start
        for k, pid in enumerate(seq):
            a = cursor_t
            b = end if k == len(seq) - 1 else start + (end - start) * sum(shares[:k + 1])
            cursor_t = b
            add({"start": a, "end": b, "panels": [pid], "cues": [], "emphasis": [],
                 "sentence": seg.get("text") if k == 0 else None,
                 "sentence_starts": [start] if k == 0 else [a]})
    if units:
        units[0]["start"] = 0.0
    return units


def hold_groups(units: List[Dict[str, Any]], min_hold: float = MIN_HOLD, *,
                lead: float | None = None, max_late: float | None = None) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """Pack timed single-panel units into shots with a bounded cut budget.

    Every cut may move only inside [sentence_start - lead, sentence_start +
    max_late]. A picture can no longer borrow seconds from a far-away
    neighbour - the old donor pass was what made the shot drift away from the
    voice. A unit shorter than min_hold shares one shot with the next unit
    (two cards, the second arriving on its own words); a shot still shorter
    than min_hold after that takes at most `lead` more from its start
    and `max_late` from its end. A shot that even then can't reach the floor
    is left short and counted in stats["short_shots"] - the plan QA reports
    it and the sync gate FAILS on it; the timing never silently drifts to
    hide it.

    Returns (shots, stats). `lead` and `max_late` default to
    the video.sync values (sync.SYNC_DEFAULTS); build_plan passes the effective
    settings from manhwa-ocr.yaml.
    """
    lead = float(sync_mod.SYNC_DEFAULTS["lead"]) if lead is None else float(lead)
    max_late = float(sync_mod.SYNC_DEFAULTS["max_late"]) if max_late is None else float(max_late)
    min_hold = float(min_hold)
    # The floor IS min_hold: no second, lower floor exists anymore. The old
    # min_hold_short (2.0s) exception is deleted — a shot under min_hold is
    # a real failure (counted in short_shots, failed by the sync gate), not
    # a "short hold" that gets a pass.
    floor = min_hold
    if not units:
        return [], {"min_hold": min_hold, "shots": 0, "pairs": 0, "nudged": 0,
                    "moved_seconds": 0.0, "short_shots": 0}

    def span(g: List[Dict[str, Any]]) -> float:
        return float(g[-1]["end"]) - float(g[0]["start"])

    groups: List[List[Dict[str, Any]]] = []
    i = 0
    while i < len(units):
        if float(units[i]["end"]) - float(units[i]["start"]) < min_hold - 1e-6 and i + 1 < len(units):
            groups.append([units[i], units[i + 1]])
            i += 2
        else:
            groups.append([units[i]])
            i += 1

    # A lone short tail after a pair sometimes fits better as that pair's
    # second card (its first panel becomes the short one): slide the last
    # three units while that lowers the total shortfall.
    while len(groups) >= 2 and len(groups[-1]) == 1 and len(groups[-2]) == 2:
        x, y = groups[-2]
        z = groups[-1][0]
        now = max(0.0, floor - span(groups[-2])) + max(0.0, floor - span(groups[-1]))
        cand = [[x], [y, z]]
        better = sum(max(0.0, floor - span(c)) for c in cand)
        if better < now - 1e-9:
            groups[-2:] = cand
        else:
            break

    n = len(groups)
    b = [0.0] + [float(g[0]["start"]) for g in groups[1:]] + [float(groups[-1][-1]["end"])]

    # Every cut sits inside its own budget and in order (shots never overlap).
    tight = 0
    for k in range(1, n):
        s = float(groups[k][0]["start"])
        b[k] = min(max(b[k], s - lead), s + max_late)
        if b[k] < b[k - 1] + 0.2:
            b[k] = b[k - 1] + 0.2
            tight += 1

    nudged, moved, short_shots = 0, 0.0, 0
    for k in range(n):
        if b[k + 1] - b[k] >= floor - 1e-6:
            continue
        want = floor - (b[k + 1] - b[k])
        before = b[k + 1] - b[k]
        if k + 1 < n:                       # take from the end first (the next shot is often long)
            room = (float(groups[k + 1][0]["start"]) + max_late) - b[k + 1]
            take = min(want, max(0.0, room))
            if take > 1e-9:
                b[k + 1] += take
                want -= take
        if want > 1e-9:                     # then from the start, sparing the previous shot's floor
            room = b[k] - (float(groups[k][0]["start"]) - lead)
            if k > 0:
                room = min(room, b[k] - (b[k - 1] + 0.2),
                           max(0.0, (b[k] - b[k - 1]) - floor))
            take = min(want, max(0.0, room))
            if take > 1e-9:
                b[k] -= take
        after = b[k + 1] - b[k]
        if after > before + 1e-9:
            nudged += 1
            moved += after - before
        if after < floor - 1e-6:
            short_shots += 1

    shots: List[Dict[str, Any]] = []
    pairs = 0
    for k, g in enumerate(groups):
        start, end = b[k], b[k + 1]
        if len(g) == 1:
            shots.append({**g[0], "start": start, "end": end})
            continue
        pairs += 1
        a, c = g
        shots.append({"start": start, "end": end, "panels": [a["panels"][0], c["panels"][0]],
                      "cues": a["cues"] + c["cues"], "emphasis": a["emphasis"] + c["emphasis"],
                      "sentence": " ".join(x for x in (a.get("sentence"), c.get("sentence")) if x) or None,
                      "sentence_starts": a["sentence_starts"] + c["sentence_starts"],
                      "second_at": c["start"] - a["start"]})
    shots[0]["start"] = 0.0
    shots[-1]["end"] = units[-1]["end"]
    stats = {"min_hold": min_hold, "shots": len(shots), "pairs": pairs,
             "short_floor": floor, "nudged": nudged, "moved_seconds": round(moved, 3),
             "short_shots": short_shots, "tight_cuts": tight}
    return shots, stats


def shot_frame_bounds(shots, fps, duration):
    """Exact (f0, f1) frame bounds for each shot: contiguous, no gap, no overlap.

    Joins are frame-exact by construction: each shot starts on the frame the
    previous shot ends on, and the last shot ends on the video's final frame.
    A shot that rounds to zero frames is an error (the renderer would have
    no frame to draw for it). Concat is a hard cut by design -- there is
    no crossfade, so no frame is ever blended across a cut.
    """
    fps = int(fps)
    bounds = [round(float(s["start"]) * fps) for s in shots] + [round(float(duration) * fps)]
    out = []
    for k, s in enumerate(shots):
        f0, f1 = bounds[k], bounds[k + 1]
        if f1 <= f0:
            raise UserError(f"Shot {s.get('id')} is shorter than one frame. Lengthen it in the plan.")
        out.append((f0, f1))
    return out


def ensure_split_crop_change(shots):
    """NEVER split a long sentence into multiple shots with the SAME panel and
    the SAME crop: the entrance restarts and the cut reads as a glitch.

    Walks the shot list; when a shot shows the same single panel with an
    identical crop to the shot before it, the later shot is punched in 8%
    (same helper split_long_shot uses) and a restarting pan becomes push_in.
    Returns (shots, changes) with one reason-logged entry per fix. Shots that
    already differ in crop are left alone.
    """
    changes = []
    for k in range(1, len(shots)):
        prev_cards = shots[k - 1].get("cards") or []
        cur_cards = shots[k].get("cards") or []
        if len(prev_cards) != 1 or len(cur_cards) != 1:
            continue
        if prev_cards[0].get("panel") != cur_cards[0].get("panel"):
            continue
        prev_crop = list(prev_cards[0].get("crop") or [0, 0, 1, 1])
        cur_crop = list(cur_cards[0].get("crop") or [0, 0, 1, 1])
        if any(abs(a - b) > 1e-9 for a, b in zip(prev_crop, cur_crop)):
            continue  # already a different crop: the cut reads as a punch-in
        new_crop = _inset_crop(cur_crop, 0.08)
        if new_crop == cur_crop:
            continue  # already at the minimum readable crop: nothing more to punch into
        cur = dict(cur_cards[0], crop=new_crop)
        if cur.get("motion") in PAN_DIRECTIONS:
            cur["motion"] = "push_in"  # a pan would restart q -1 -> +1 on the new shot
        shots[k] = {**shots[k], "cards": [cur]}
        changes.append({"shot": shots[k].get("id"), "panel": cur.get("panel"),
                        "reason": ("same panel + same crop as the previous shot: punched in 8% "
                                   "(pan -> push_in) so the cut reads as a crop change, not a frozen restart")})
    return shots, changes


def ensure_motion_variety(shots):
    """Final no-repeat pass: no two consecutive shots share a first-card motion.

    The designer already avoids repeats, and split_long_shot alternates
    piece motions -- but a split shot's LAST piece can still land on the
    same motion as the NEXT shot's first card (the designer never saw
    the flipped piece motion). This pass flips the later shot's
    first-card motion (push_in <-> pull_out, pan -> push_in) so the
    invariant holds across the final shot list. Hand-set `hold` is
    never flipped (a payoff beat). Returns (shots, changes).
    """
    changes = []
    for k in range(1, len(shots)):
        prev_cards = shots[k - 1].get("cards") or []
        cur_cards = shots[k].get("cards") or []
        if not prev_cards or not cur_cards:
            continue
        prev_motion = prev_cards[0].get("motion")
        cur_motion = cur_cards[0].get("motion")
        if cur_motion is None or cur_motion != prev_motion or cur_motion == "hold":
            continue
        new_motion = "push_in" if cur_motion in PAN_DIRECTIONS else (
            "pull_out" if cur_motion == "push_in" else "push_in")
        cur = dict(cur_cards[0], motion=new_motion)
        shots[k] = {**shots[k], "cards": [cur, *cur_cards[1:]]}
        changes.append({"shot": shots[k].get("id"), "from": cur_motion, "to": new_motion,
                        "reason": "first-card motion repeated the previous shot; flipped so no two shots drift the same way"})
    return shots, changes


def ensure_panel_diversity(shots):
    """Never show the same panel 3x consecutively with an identical crop.

    Impactful fillers and repeated tags can leave one panel on screen for
    three sentences in a row (Ch10 test build: P014-10 for sentences
    55/56/57, P020-01 for 75/76/77 -- correct pictures, visibly
    repetitive). When a shot's first-card panel matches the previous
    two shots' first-card panel, the third shot's crop is punched in
    until it differs from BOTH predecessors' crops (a different crop of
    the same panel; a different panel is the alignment's call, not the
    planner's -- the planner never re-picks a panel). A shot whose crop
    already differs from both predecessors is left alone. Returns
    (shots, changes) with one reason-logged entry per fix.
    """
    changes = []
    for k in range(2, len(shots)):
        cards = shots[k].get("cards") or []
        if not cards:
            continue
        panel = cards[0].get("panel")
        prev1 = (shots[k - 1].get("cards") or [{}])[0]
        prev2 = (shots[k - 2].get("cards") or [{}])[0]
        if panel is None or prev1.get("panel") != panel or prev2.get("panel") != panel:
            continue
        prev_crops = [list(prev1.get("crop") or [0, 0, 1, 1]),
                      list(prev2.get("crop") or [0, 0, 1, 1])]
        cur_crop = list(cards[0].get("crop") or [0, 0, 1, 1])

        def _differs(crop):
            return all(any(abs(a - b) > 1e-9 for a, b in zip(crop, pc)) for pc in prev_crops)

        if _differs(cur_crop):
            continue  # already a different crop from both predecessors
        new_crop = None
        for frac in (0.08, 0.16, 0.24, 0.32):
            cand = _inset_crop(cur_crop, frac)
            if cand != cur_crop and _differs(cand):
                new_crop = cand
                break
        if new_crop is None:
            continue  # already at the minimum readable crop: nothing more to punch into
        cur = dict(cards[0], crop=new_crop)
        if cur.get("motion") in PAN_DIRECTIONS:
            cur["motion"] = "push_in"  # a pan would restart q -1 -> +1 on the new shot
        shots[k] = {**shots[k], "cards": [cur, *cards[1:]]}
        changes.append({"shot": shots[k].get("id"), "panel": panel,
                        "reason": ("same panel as the previous two shots: punched in so the third "
                                   "showing is a different crop, not the same card a third time")})
    return shots, changes


def split_system_window_units(units, by_id):
    """Split a 2-panel unit that contains a system-window panel.

    A system-window banner must get its own WIDE shot (see
    crops.is_system_window): the pair layout would cut it to a small
    square. hold_groups() / _pair_up() may still group one with a
    neighbour (a short sentence sharing a shot), so this pass -- run
    after pairing, before the designer -- splits such a unit into two
    single-panel units at the second panel's arrival (unit['second_at'],
    the same instant the pair shot would have slid it in). The first
    piece keeps the unit's sentence/cues; the second gets sentence None
    (its caption comes from the alignment sentences, never from shots).
    Units without a system-window panel pass through untouched.
    """
    out = []
    for u in units:
        panels = list(u.get("panels") or [])
        if len(panels) == 2 and any(is_system_window(by_id.get(pid) or {}) for pid in panels):
            try:
                cut = float(u.get("second_at") or 0)
            except (TypeError, ValueError):
                cut = 0.0
            start, end = float(u["start"]), float(u["end"])
            cut_t = start + cut if 0 < cut < (end - start) else start + (end - start) / 2
            first = {**u, "end": cut_t, "panels": panels[:1]}
            second = {**u, "start": cut_t, "panels": panels[1:],
                      "sentence": None, "cues": []}
            first["sentence_starts"] = [s for s in (u.get("sentence_starts") or []) if s <= cut_t] or [start]
            second["sentence_starts"] = [s for s in (u.get("sentence_starts") or []) if s > cut_t] or [cut_t]
            first.pop("second_at", None)
            second.pop("second_at", None)
            out.extend([first, second])
            continue
        out.append(u)
    return out


# ------------------------------------------------------------------ shot design


def map_options(vcfg: Dict[str, Any]) -> Dict[str, Any]:
    """Crop-analysis options from the video config: video.crop and video.panels thresholds."""
    crop = vcfg.get("crop") if isinstance(vcfg.get("crop"), dict) else {}
    panels = vcfg.get("panels") if isinstance(vcfg.get("panels"), dict) else {}
    heads = vcfg.get("heads") if isinstance(vcfg.get("heads"), dict) else {}
    return {"full_width": bool(crop.get("full_width", True)),
            "min_width_kept": float(crop.get("min_width_kept", 0.9)),
            "white_threshold": int(panels.get("white_threshold", 242)),
            "black_threshold": int(panels.get("black_threshold", 13)),
            "pale_heads": bool(heads.get("pale", True)),
            "keep_top": float(heads.get("keep_top", 0.25))}


def background_for_index(i: int, layout: str = "single") -> str:
    """Deterministic card-background rotation for shot index i (0-based).

    i % 10 == 4 -> "black" (solid black, no blur)
    i % 10 == 9 -> "white" (solid white, no blur)
    otherwise -> "blur" (auto-blur backdrop: existing blurred + darkened +
    vignetted panel backdrop from frames.background()).

    The background colour only replaces the blurred backdrop BEHIND the
    card; the card (panel art) is unchanged, centred, full card size, and
    its colours are never inverted. Chapter cards / end-screen / black
    tail are not shots, so this rotation never applies to them.
    """
    if layout == "inset":
        return "blur"
    if i % 10 == 4:
        return "black"
    if i % 10 == 9:
        return "white"
    return "blur"


class _Designer:
    """Turns timing units into shots: layouts, crops, backgrounds, animation."""

    def __init__(self, chapter: str, panels: List[Dict[str, Any]], fmt: Format, vcfg: Dict[str, Any],
                 image_for: Callable[[Dict[str, Any]], Image.Image], fixes: Dict[str, Any] | None = None,
                 root: Path | None = None, fingerprint: str | None = None):
        self.chapter, self.fmt, self.vcfg = chapter, fmt, vcfg
        self._root, self._fingerprint = root, fingerprint
        self.fixes = {"crop": {}, "face": {}, "shot": {}, **(fixes or {})}
        self.by_id = {p["id"]: p for p in panels}
        self.image_for = image_for
        self._img: "OrderedDict[str, Image.Image]" = OrderedDict()
        self._crops: Dict[tuple, List[float]] = {}
        self._pads: Dict[tuple, float] = {}
        self._maps: Dict[str, Any] = {}
        self.crop_stats: Dict[str, List[Any]] = {"head_risk": [], "side_cut": [], "bubble": [],
                                                 "padded": [], "white_edge": [],
                                                 "low_confidence_heads": [],
                                                 # P1: panels with a dominant bubble overlapping
                                                 # the head range (mask risk). Informational:
                                                 # dropping happens in verify.py; here the
                                                 # clean crop (if one exists) already avoids it.
                                                 "mask_risk": [],
                                                 # P2: crops the planner auto-switched to the
                                                 # pair-safe shape (see crop()). Each entry:
                                                 # {"panel", "from_shape", "to_shape", "reason"}.
                                                 "layout_switches": []}
        self.map_options = map_options(vcfg)
        self._tone: Dict[str, str] = {}
        self.uses: Dict[str, int] = {}
        self.prev_enter: str | None = None
        self.prev_motion: str | None = None
        self._prev_shot: Dict[str, Any] | None = None   # CapCut transitions: the shot before this one
        self.last_glitch = -1e9                          # CapCut: at most one glitch per 60s
        self.last_punch = -1e9

    def img(self, pid: str) -> Image.Image:
        if pid in self._img:
            self._img.move_to_end(pid)
            return self._img[pid]
        self._img[pid] = self.image_for(self.by_id[pid])
        # LRU: evict one entry, never wipe the whole cache (the old
        # `clear()` made the planner re-decode panels it had just loaded).
        while len(self._img) > 32:
            self._img.popitem(last=False)
        return self._img[pid]

    def panel_aspect(self, pid: str) -> float:
        p = self.by_id[pid]
        return float(p.get("width") or 1) / max(1.0, float(p.get("height") or 1))

    def pad(self, pid: str, layout: str) -> float:
        """Blurred padding above and below the card's crop, in panel heights (0 for most cards)."""
        self.crop(pid, layout)
        return self._pads.get((pid, layout), 0.0)

    def _fixes_crop_for_layout(self, pid: str, layout: str, box: List[float]) -> List[float]:
        """A reviewer's fixes.json crop is layout-blind: the same box is reused
        in every layout. If its aspect is not an allowed shape for THIS layout
        (Ch4: a 4:3 crop failing QA once the panel landed in a pair), P2
        auto-switches to a clean crop in this layout's shapes when one exists
        and records the switch. If no clean crop exists the reviewer's crop
        stands (a human chose it) and QA keeps flagging the shape."""
        _p = self.by_id[pid]
        pw, ph = float(_p.get("width") or 1), float(_p.get("height") or 1)
        w = (box[2] - box[0]) * pw
        h = (box[3] - box[1]) * ph
        if h <= 0:
            return list(box)
        aspect = w / h
        allowed = shapes_for(layout, self.fmt)
        if any(abs(math.log(aspect / a)) <= 0.02 for a in allowed):
            return list(box)
        if pid not in self._maps:
            self._maps[pid] = _panel_maps(self.img(pid), self.map_options)
        card = pick_card(self.img(pid), list(allowed), self._maps[pid])
        if not card["clean"]:
            return list(box)
        self.crop_stats["layout_switches"].append(
            {"panel": pid, "layout": layout, "from_shape": "fixes.json",
             "to_shape": float(card["shape"]), "box": list(card["box"]),
             "reason": (f"fixes.json crop aspect {aspect:.3f} is not an allowed shape for {layout}; "
                        f"auto-switched to the clean {card['shape']:.3f} crop")})
        return list(card["box"])

    def _pair_safe_switch(self, pid: str, layout: str, entry: Dict[str, Any] | None,
                          ckey: str | None) -> Dict[str, Any] | None:
        """P2 auto-switch for single/wide layouts (see verify.find_pair_safe_shape).
        Cache-aware: a stored "layout_switch" is reused without loading the image;
        a pre-P2 entry without the key is upgraded once."""
        if layout not in ("single", "wide"):
            return None
        if entry is not None and "layout_switch" in entry:
            cached = entry.get("layout_switch")
            if cached is None:
                return None
            # A cached switch recorded before the wide-layout validity
            # fix may point at a shape this layout cannot show (wide ->
            # 1.0): drop it and recompute below (now None for wide)
            # instead of reusing a square crop in a wide shot.
            _to = float(cached.get("to_shape") or 0)
            if any(abs(math.log(_to / a)) <= 0.02 for a in shapes_for(layout, self.fmt)):
                return cached
        if pid not in self._maps:
            self._maps[pid] = _panel_maps(self.img(pid), self.map_options)
        layout_shapes = {name: shapes_for(name, self.fmt) for name in ("single", "pair", "wide")}
        switch = find_pair_safe_shape(self.img(pid), self._maps[pid], layout_shapes, layout)
        if entry is not None and ckey is not None:
            store_crop(self._root, self.chapter, ckey, {**entry, "layout_switch": switch})
        return switch

    def crop(self, pid: str, layout: str) -> List[float]:
        """The card's crop: one exact shape from the layout's list (crops.py)."""
        key = (pid, layout)
        if key not in self._crops:
            if pid in self.fixes["crop"]:                  # a reviewer's exact crop (fixes.json)
                shape, box = self.fixes["crop"][pid]
                # P2: a fixes.json crop is layout-blind; switch it when its
                # aspect is not allowed in this layout and a clean crop exists.
                box = self._fixes_crop_for_layout(pid, layout, box)
                self._crops[key] = list(box)
                if box[2] - box[0] < 0.999 and pid not in self.crop_stats["side_cut"]:
                    self.crop_stats["side_cut"].append(pid)
                return self._crops[key]
            # Crop cache: on a hit, no image decode and no _panel_maps.
            shapes = shapes_for(layout, self.fmt)
            face = self.fixes["face"].get(pid)
            ckey = None
            entry = None
            if self._root is not None and self._fingerprint:
                ckey = crop_key(self._fingerprint, self.by_id[pid], shapes, self.map_options, face)
                entry = load_crop(self._root, self.chapter, ckey)
            if entry is not None:
                card = entry["pick"]
                head = entry["head"]
                top, bottom = entry["edge_rows"]
                # P1: mask risk rides in the cache entry (or, for pre-P1
                # entries, in the pick's problems). Recorded in crop_stats
                # below, next to head_risk / bubble.
                hit_risk = entry.get("mask_risk", "mask_risk" in card.get("problems", []))
                if (top + bottom) >= 0.01 and pid not in self.crop_stats["white_edge"]:
                    # Gutter rows still sit on this panel's edge (panels.json predates edge
                    # tightening). `video panels --force` re-detects it flush to the art.
                    self.crop_stats["white_edge"].append(pid)
            else:
                if pid not in self._maps:
                    self._maps[pid] = _panel_maps(self.img(pid), self.map_options)
                    top, bottom = self._maps[pid]["edge_rows"]
                    if (top + bottom) >= 0.01 and pid not in self.crop_stats["white_edge"]:
                        # Gutter rows still sit on this panel's edge (panels.json predates edge
                        # tightening). `video panels --force` re-detects it flush to the art.
                        self.crop_stats["white_edge"].append(pid)
                maps = self._maps[pid]
                if face is not None:                   # a reviewer's head position replaces the skin guess
                    maps = {**maps, "face_override": face}
                head = head_of(maps)
                card = pick_card(self.img(pid), shapes, maps)
                if ckey is not None:
                    store_crop(self._root, self.chapter, ckey,
                               {"pick": card, "head": head, "edge_rows": list(self._maps[pid]["edge_rows"]),
                                "mask_risk": bool(is_mask_risk(self._maps[pid]))})
            if not head["confident"] and (head["source"] == "pale" or head["keep_top"]) \
                    and pid not in self.crop_stats["low_confidence_heads"]:
                self.crop_stats["low_confidence_heads"].append(pid)
            shape, box, probs = card["shape"], card["box"], card["problems"]
            # P2 auto-switch: when single/wide and pair crops are both clean
            # but different shapes, use the pair-safe (narrower) shape here
            # too, so one crop passes QA in every layout. Recorded in
            # crop_stats["layout_switches"] (surfaced in plan + QA, never silent).
            switch = self._pair_safe_switch(pid, layout, entry, ckey)
            if switch is not None and abs(float(switch["to_shape"]) - float(shape)) > 1e-9:
                box = list(switch["box"])
                probs = []
                shape = float(switch["to_shape"])
                card = {**card, "shape": shape, "box": box,
                        "pad_y": float(switch.get("pad_y") or 0.0), "problems": []}
                if not any(s.get("panel") == pid and s.get("layout") == layout
                           for s in self.crop_stats["layout_switches"]):
                    self.crop_stats["layout_switches"].append(
                        {"panel": pid, "layout": layout,
                         "from_shape": float(switch["from_shape"]), "to_shape": shape,
                         "reason": switch.get("reason", "")})
            if ("mask_risk" in probs or (entry is not None and hit_risk)) \
                    and pid not in self.crop_stats["mask_risk"]:
                self.crop_stats["mask_risk"].append(pid)
            if card["pad_y"] > 0:
                self._pads[key] = float(card["pad_y"])
            if "head_cut" in probs and pid not in self.crop_stats["head_risk"]:
                self.crop_stats["head_risk"].append(pid)
            if "lettering" in probs and pid not in self.crop_stats["bubble"]:
                self.crop_stats["bubble"].append(pid)
            if box[2] - box[0] < 0.999 and pid not in self.crop_stats["side_cut"]:
                self.crop_stats["side_cut"].append(pid)
            if card["pad_y"] > 0 and pid not in self.crop_stats["padded"]:
                self.crop_stats["padded"].append(pid)
            self._crops[key] = list(box)
        return self._crops[key]

    def tone(self, pid: str) -> str:
        if pid not in self._tone:
            self._tone[pid] = tone(self.img(pid))
        return self._tone[pid]

    def background(self, i: int, layout: str) -> str:
        """Deterministic background rotation (user rule, 2026-10-19).

        Shot index i (0-based, plan order): i % 10 == 4 -> black,
        i % 10 == 9 -> white, else auto-blur backdrop. Exactly 80% blur /
        10% black / 10% white over every 10 shots, evenly spaced (black
        and white never consecutive, never clustered), same plan -> same
        backgrounds, no randomness, no seed. Inset shots (never emitted)
        stay blur. An explicit fixes.json shot background override in
        shot() still wins over this rotation.
        """
        return background_for_index(i, layout)

    def motion(self, rng: random.Random, crop: List[float]) -> str:
        # P3 + CapCut bugfix: continuous motion, never `hold` (planner-emitted).
        # Never repeat the previous motion, and never pan twice in a row: the
        # old code avoided only the exact previous key, so a previous
        # "pan_left" did not exclude the "pan" group and the planner could
        # emit pan_right, pan_left, pan_right, each restarting q -1 -> +1.
        avoid = "pan" if self.prev_motion in PAN_DIRECTIONS else self.prev_motion
        picked = _pick(rng, MOTION_WEIGHTS, avoid=avoid)
        if picked != "pan":
            self.prev_motion = picked
            return picked
        if crop[3] - crop[1] > 0.999:
            self.prev_motion = "push_in"
            return "push_in"     # the whole panel is on the card: nothing to pan over
        dirs = [d for d in PAN_DIRECTIONS if d != self.prev_motion]
        picked = rng.choice(dirs)
        self.prev_motion = picked
        return picked

    def entrance(self, rng: random.Random, cues: List[str], layout: str) -> str:
        if "reveal" in cues:
            e = rng.choice(["blur_in", "fit_in_frame"])
        elif "impact" in cues:
            e = "zoom_in"
        elif "beat" in cues:
            e = "fade_in"
        elif layout == "wide":
            e = rng.choice(["cut", "cut", "fade_in", "slide_left"])
        else:
            e = _pick(rng, ENTRANCE_WEIGHTS, avoid=self.prev_enter)
        self.prev_enter = e
        return e

    def shot(self, i: int, unit: Dict[str, Any], last: bool) -> Dict[str, Any]:
        rng = _rng(self.chapter, i)
        start, end = unit["start"], unit["end"]
        dur = end - start
        ids = [p for p in dict.fromkeys(unit["panels"]) if p in self.by_id]
        first = ids[0]

        if len(ids) == 1 and is_system_window(self.by_id.get(first) or {}):
            # A system-window banner gets its own WIDE shot (never a
            # pair: the pair layout would cut it to a small square).
            layout = "wide"
        elif len(ids) >= 2:
            # Both panels are cards, whatever the shot's length (hold_groups() has
            # already given it the short floor). The inset layout would turn one
            # panel into a blurred background only, so it is never used here.
            layout = "pair"
        elif self.panel_aspect(first) >= 1.45:
            layout = "wide"
        else:
            layout = "single"

        cards: List[Dict[str, Any]] = []
        bg_panel = None
        if layout == "inset":
            bg_panel, card_ids = ids[0], [ids[1]]
        elif layout == "pair":
            card_ids = ids[:2]
        else:
            card_ids = [first]

        for k, pid in enumerate(card_ids):
            crop = self.crop(pid, layout)
            if layout == "pair" and k == 1:
                # The second card arrives exactly on its own words
                # (unit["second_at"] is its own unit's start, relative to
                # the shot). There used to be a 0.6s floor here
                # (max(0.6, second_at)): on a boundary pair — two sentences
                # ~0.06s apart — that floor pushed the second card to
                # ~0.66s late, past the gate (max_late 0.3 + grace 0.2 =
                # 0.5s), a structurally unavoidable late cut. No floor:
                # arrival = shot start + second_at, and the shot start
                # itself is inside the first sentence's cut budget, so a
                # second-card arrival can never be structurally late.
                # The dur - 1.0 cap only pulls it EARLIER (never late) when
                # the second piece itself is under a second.
                if unit.get("second_at") is not None:   # arrives on its own words
                    at = round(max(0.0, min(float(unit["second_at"]), dur - 1.0)), 3)
                else:
                    at = round(max(0.0, min(max(0.5, 0.45 * dur), dur - 1.2)), 3)
                enter = "slide_up" if self.fmt.portrait else "slide_left"
                motion = "push_in"
            else:
                at = 0.0
                enter = self.entrance(rng, unit["cues"], layout)
                if "beat" in unit["cues"]:
                    motion = "push_in"
                    self.prev_motion = "push_in"  # a forced motion still counts as previous
                else:
                    motion = self.motion(rng, crop)
            enter = (self.fixes["shot"].get(pid) or {}).get("enter", enter)   # fixes.json "shot" override
            card = {"panel": pid, "crop": crop, "enter": enter, "at": at, "motion": motion}
            if len(card_ids) == 1 and is_system_window(self.by_id.get(pid) or {}):
                card["wide_fill"] = True  # fill >=90% of the frame width, never a small square
            pad = self.pad(pid, layout)
            if pad > 0:
                card["pad_y"] = round(pad, 4)       # a wide panel: blurred padding above and below
            cards.append(card)

        effects: List[Dict[str, Any]] = []
        on = {"shake": True, "punch": False, "flash": False}
        on.update(self.vcfg.get("effects") or {})
        lo, hi = start + EFFECT_GRACE, end - 0.25
        cue_t = next((t for t in unit["sentence_starts"] if lo <= t <= hi), lo if lo <= hi else None)
        if cue_t is not None:
            # Let an entrance finish first, so the shake reads as a hit, not a glitch.
            hit = min(hi, max(cue_t, start + 0.55))
            if "impact" in unit["cues"] and on.get("shake"):
                effects.append({"type": "shake", "t": round(hit, 3)})
            if "reveal" in unit["cues"] and on.get("flash"):
                effects.append({"type": "flash", "t": round(hit, 3)})
        if on.get("punch"):
            for t in sorted(unit["emphasis"]):
                if lo <= t <= hi and t - self.last_punch >= PUNCH_GAP:
                    effects.append({"type": "punch", "t": round(t, 3)})
                    self.last_punch = t

        # P3: one keyword callout on impact / reveal shots (editor.py).
        callouts: List[Dict[str, Any]] = []
        _call = extract_callout(unit.get("sentence") or "", unit.get("cues") or [])
        if _call is not None:
            callouts.append({"t": round(start + 0.4, 3), "text": _call["text"],
                             "style": _call["style"], "dur": 1.2})
        # CapCut transitions: how this shot ARRIVES (frames.TRANSITIONS).
        # Hard cut default (90%), fade 10%; slide / zoom / whip / glitch
        # only on impact / reveal beats, glitch at most once per 60s.
        # A fade also fades the PREVIOUS shot out; a glitch adds a 0.2s
        # RGB-split effect at the cut. The first shot always hard-cuts in.
        transition = "cut"
        if self._prev_shot is not None:
            if "impact" in unit["cues"] or "reveal" in unit["cues"]:
                _pool = ["slide", "zoom", "whip"]
                if start - self.last_glitch >= GLITCH_MIN_GAP:
                    _pool.append("glitch")
                transition = rng.choice(_pool)
            else:
                transition = _pick(rng, TRANSITION_WEIGHTS)
            if transition == "fade":
                self._prev_shot["exit"] = "fade_out"
                cards[0]["enter"] = "fade_in"
            elif transition != "cut":
                cards[0]["enter"] = transition_enter(transition)
            if transition == "glitch":
                effects.append({"type": "glitch", "t": round(start, 3)})
                self.last_glitch = start
        background = self.background(i, layout)
        background = (self.fixes["shot"].get(first) or {}).get("background", background)
        shot = {
            "id": f"S{i + 1:03d}", "start": round(start, 3), "end": round(end, 3),
            "layout": layout, "background": background,
            "cards": cards, "exit": "fade_out" if last else "cut", "effects": effects,
            "transition": transition,
            "callouts": callouts,
            "sentence": (unit.get("sentence") or "")[:300] or None,
        }
        if self.vcfg.get("grade") == "subtle":
            shot["grade"] = "subtle"  # CapCut grade, off by default (video.grade)
        self._prev_shot = shot
        if bg_panel:
            shot["bg_panel"] = bg_panel
        return shot


def _pair_up(units: List[Dict[str, Any]], chapter: str, ratio: float, by_id: Dict[str, Any],
             timed: bool = False) -> List[Dict[str, Any]]:
    """Merge some neighbouring panels into two-card shots: the first card
    starts centred, then slides left as the second arrives from the right.

    The second card arrives exactly when its own unit would have started, so
    with synced timing every picture still lands on its words."""
    out: List[Dict[str, Any]] = []
    i = 0
    while i < len(units):
        u = units[i]
        if i + 1 < len(units):
            v = units[i + 1]
            a, b = by_id.get(u["panels"][0], {}), by_id.get(v["panels"][0], {})
            # Pair cards are narrow and panels are never cut at the sides, so
            # only portrait-shaped panels pair up.
            wide = lambda p: float(p.get("width") or 1) / max(1.0, float(p.get("height") or 1)) > 1.0  # noqa: E731
            # Synced: each card must be on screen long enough for its words, but not too long.
            long_enough = (not timed or (1.2 <= u["end"] - u["start"] <= 4.0 and 1.5 <= v["end"] - v["start"] <= 4.0))
            # Only neighbours in story order pair up; impact/reveal stay solo
            # so their hit reads instead of sharing a card.
            story_close = True
            try:
                all_ids = list(by_id.keys())
                story_close = abs(all_ids.index(u["panels"][0]) - all_ids.index(v["panels"][0])) <= 6
            except ValueError:
                story_close = True
            solo_cues = set(u.get("cues") or []) | set(v.get("cues") or [])
            same_sentence = (u.get("sentence") or "") == (v.get("sentence") or "") and bool(u.get("sentence"))
            if (long_enough and len(u["panels"]) == 1 and len(v["panels"]) == 1
                    and u["panels"][0] != v["panels"][0] and not wide(a) and not wide(b)
                    and story_close and (same_sentence or not (solo_cues & {"impact", "reveal"}))
                    and (2.8 <= (v["end"] - u["start"]) <= MAX_PAIR_SHOT)
                    and _rng(chapter, 10_000 + i).random() < ratio):
                # The second card slides in exactly when its own part starts.
                out.append({**u, "end": v["end"], "panels": [u["panels"][0], v["panels"][0]],
                            "second_at": v["start"] - u["start"],
                            "cues": u["cues"] + v["cues"], "emphasis": u["emphasis"] + v["emphasis"],
                            "sentence": " ".join(x for x in (u.get("sentence"), v.get("sentence")) if x) or None,
                            "sentence_starts": u["sentence_starts"] + v["sentence_starts"]})
                i += 2
                continue
        out.append(u)
        i += 1
    return out


def _cinematic_pass(shots: List[Dict[str, Any]], units: List[Dict[str, Any]],
                    designer: "_Designer", ccfg: Dict[str, Any],
                    characters: List[str] | None) -> List[Dict[str, Any]]:
    """Convert classic card shots into cinematic full-screen shots.

    - Marks every shot ``cinematic: True`` and assigns the cinematic layout
      (tall / fullbleed) from the panel's aspect ratio. The classic
      ``layout``/``background`` keys are left valid ("single"/"blur") so
      validate(), QA and review tooling keep working; render.py swaps in
      CinematicShot for shots carrying the cinematic flag.
    - Hard cuts only: transition/exit forced to "cut" (except the final
      end-fade, which render handles).
    - Zoom direction alternates per shot for motion variety.
    - Name cards: the first shot whose sentence first mentions a known
      character gets a lower-third name overlay.
    - Reframes: a shot longer than 5.5s is split at its midpoint into two
      shots of the same panel with different focal points (>= 2.0s each),
      giving the fast-cut feel of the reference videos without breaking
      narration sync (the sync gate counts same-panel reframes as correct).
    """
    import re
    chars = [c for c in (characters or []) if c and len(c) >= 2]
    # Longest names first so "Kim Jin-Ho" wins over "Kim".
    chars.sort(key=len, reverse=True)
    patterns = [(c, re.compile(r"\b" + re.escape(c) + r"\b", re.IGNORECASE)) for c in chars]
    seen: set = set()
    zoom_flip = 1
    out: List[Dict[str, Any]] = []
    reframe_at = 3.2
    floor = max(1.2, float(ccfg.get("min_hold", 1.5)))

    def _reframe_pieces(shot: Dict[str, Any], name_card: str | None) -> List[Dict[str, Any]]:
        """Recursively split a long shot into reframe pieces."""
        dur = float(shot["end"]) - float(shot["start"])
        if not (ccfg.get("reframe", True) and dur > reframe_at):
            return [shot]
        mid = round(shot["start"] + dur / 2, 3)
        if mid - shot["start"] < floor or shot["end"] - mid < floor:
            return [shot]
        first = dict(shot)
        first.update({"end": mid, "focus": (0.5, 0.35), "name_card": name_card})
        second = dict(shot)
        second.update({"start": mid, "zoom_dir": -shot["zoom_dir"],
                       "focus": (0.5, 0.65), "name_card": None, "reframe": True,
                       # One-shot overlays stay on the first piece only.
                       "callout": None, "black_flash_in": False})
        # Alternate focal heights per piece so consecutive reframes differ.
        pieces = _reframe_pieces(first, name_card) + _reframe_pieces(second, None)
        for k, p in enumerate(pieces[1:], 1):
            p["focus"] = (0.5, 0.35 if k % 2 else 0.65)
        return pieces

    for i, shot in enumerate(shots):
        unit = units[i] if i < len(units) else {}
        cards = shot.get("cards") or []
        pid = cards[0].get("panel") if cards else None
        aspect = designer.panel_aspect(pid) if pid else 1.0
        clayout = _cinematic_layout_for(aspect)
        text = str(unit.get("sentence") or "")
        name_card = None
        if ccfg.get("name_cards", True) and text:
            for cname, pat in patterns:
                if cname.lower() not in seen and pat.search(text):
                    name_card = cname
                    seen.add(cname.lower())
                    break
        base = dict(shot)
        base.update({
            "cinematic": True,
            "cinematic_layout": clayout,
            "zoom_dir": zoom_flip,
            "zoom": float(ccfg.get("zoom", 0.06)),
            "transition": "cut",
            "exit": "cut",
            "name_card": name_card,
            "name_card_at": 0.15,
        })
        # Emphasis callout (report: large white outlined keyword like "172?"
        # upper-right on impact/reveal beats). Reuses the classic keyword
        # extractor; the cinematic renderer draws it without a black bar.
        _co = extract_callout(text, unit.get("cues") or [])
        if _co and ccfg.get("callouts", True):
            base["callout"] = _co["text"]
            base["callout_at"] = 0.3
        # Cold open: the first shot flashes in from black (the reference
        # opens straight into action; a 0.12s flash-in reads as a hard cut).
        if i == 0:
            base["black_flash_in"] = True
        base.pop("caption_bar", None)
        zoom_flip = -zoom_flip

        if len(cards) == 1:
            out.extend(_reframe_pieces(base, name_card))
        else:
            out.append(base)
    # Re-number shot ids so they stay sequential.
    for j, s in enumerate(out):
        s["id"] = f"S{j + 1:03d}"
    return out


def build_plan(*, chapter: str, panels_data: Dict[str, Any], duration: float, audio: str | None,
               vcfg: Dict[str, Any], image_for: Callable[[Dict[str, Any]], Image.Image],
               sentences: List[Dict[str, Any]] | None = None,
               sync_report: Dict[str, Any] | None = None,
               alignment_units: List[Dict[str, Any]] | None = None,
               fixes: Dict[str, Any] | None = None,
               alignment_meta: Dict[str, Any] | None = None,
               settings: Dict[str, float] | None = None,
               root: Path | None = None,
               caption_sentences: List[Dict[str, Any]] | None = None,
               characters: List[str] | None = None) -> Dict[str, Any]:
    """Turn already-decided panel timing into shots. When `alignment_units` is
    given (the normal path: chapter_N.alignment.json via units_from_alignment()),
    this function makes no panel decisions of its own - it only lays out,
    crops and animates whatever panel each unit already names. `sentences`
    (cues.json-driven) and the no-tags `even` fallback remain only for direct
    callers/tests exercising the shot-design logic in isolation; the live
    `video plan` pipeline (build.py) always passes `alignment_units`.

    `settings` are sync.effective_settings() values; the caller (make_plan)
    prints them and warns about missing config keys. Stored in the plan meta so
    a finished video can be traced back to the numbers that made it.

    `caption_sentences` are the alignment segments ({text, start, end})
    captions are timed from -- sentence windows, never per-shot estimates
    (see captions.build_captions_from_sentences). When omitted, the
    `sentences` argument is used if it carries timings, else captions
    fall back to the legacy per-shot build.
    """
    panels = panels_data["panels"]
    fmt = format_from(vcfg.get("format", "16:9"), int(vcfg.get("fps", 30)))
    by_id = {p["id"]: p for p in panels}
    st = settings or sync_mod.effective_settings(vcfg, log=lambda _msg: None)
    # Cinematic mode: full-screen panels, faster cutting. The shot floor
    # drops to 2.0s (the viral references average ~2.7s/shot) and pair
    # layouts are disabled — one panel per shot, always.
    cinematic = vcfg.get("mode") == "cinematic"
    ccfg = vcfg.get("cinematic") if isinstance(vcfg.get("cinematic"), dict) else {}
    hold_floor = float(ccfg.get("min_hold", 2.0)) if cinematic else float(st["min_hold"])
    pair_ratio = 0.0 if cinematic else float(vcfg.get("pair_ratio", 0.3))
    timing = None
    if alignment_units is not None:
        shots_units, timing = hold_groups(alignment_units, hold_floor, lead=st["lead"],
                                          max_late=st["max_late"])
        units = _pair_up(shots_units, chapter, pair_ratio, by_id, timed=True)
        # Carries the real whisper outcome through ("synced"/"estimated"/"even"),
        # not a generic "aligned" label - video render's refusal of an
        # "estimated" plan must keep working exactly as it did before this.
        mode = (alignment_meta or {}).get("sync_mode") or "aligned"
    elif sentences:
        units = _pair_up(synced_units(panels, sentences, duration, vcfg), chapter,
                         pair_ratio, by_id, timed=True)
        mode = "synced"
    else:
        shots_units, timing = hold_groups(even_units(panels, duration, vcfg), hold_floor,
                                          lead=st["lead"], max_late=st["max_late"])
        units = _pair_up(shots_units, chapter, pair_ratio, by_id)
        mode = "even"
    # A system-window banner in a 2-panel unit gets its own wide shot:
    # split such units before the designer ever sees a pair with one.
    units = split_system_window_units(units, by_id)
    designer = _Designer(chapter, panels, fmt, vcfg, image_for, fixes,
                         root=root, fingerprint=panels_data.get("fingerprint"))
    shots = [designer.shot(i, u, i == len(units) - 1) for i, u in enumerate(units)]
    if cinematic:
        # Full-screen panels, hard cuts, reframe splits, name cards.
        shots = _cinematic_pass(shots, units, designer, ccfg, characters)
    # P3 editor layer: split frozen >8s shots with a punch-in crop change,
    # then attach chapter cards + SFX. Callouts already ride on each shot.
    _pacing_before = pacing_qa(shots)
    if cinematic:
        # Reframe splits already happened in _cinematic_pass; the classic
        # punch-in split would be invisible to the cinematic renderer.
        _nsplit = 0
    else:
        shots, _nsplit = split_long_shots(shots)
    # CapCut bugfix (defect 1d): a split must never leave two shots on the
    # same panel with the same crop -- the second shot is punched in.
    shots, _split_crop_changes = ensure_split_crop_change(shots)
    # CapCut bugfix (defect 2): final no-repeat pass -- a split shot's last
    # piece can still match the next shot's motion (see ensure_motion_variety).
    shots, _motion_changes = ensure_motion_variety(shots)
    # Diversity: never the same panel 3x in a row with an identical crop
    # (the third showing is punched in to a different crop).
    shots, _diversity_changes = ensure_panel_diversity(shots)
    # CapCut beat sync: snap interior cuts to the nearest beat (<=0.15s),
    # from video.beats or music/beats.json. No beats = no-op. Snapping
    # must never push a shot under the min_hold floor, so the clamp is
    # the plan's own min_hold (chapter 3.0s, intro 2.0s), not the 0.2s
    # the beat helper defaults to.
    _beats = resolve_beats(vcfg, root)
    _beat_info = {"enabled": False, "moved": 0, "max_shift": 0.0,
                  "reason": "no beats (no video.beats or music/beats.json)"}
    if _beats:
        shots, _beat_info = snap_cuts_to_beats(shots, _beats, min_shot=float(st["min_hold"]))
    chapter_cards = build_chapter_cards(shots, duration)
    # P5: reviewer overrides from fixes.json (chapter-card titles, callout
    # text, keyed by shot id). Unknown shot_ids are warnings, never failures.
    from .shorts import apply_callout_overrides as _apply_callouts
    from .shorts import apply_card_overrides as _apply_cards
    _over = fixes or {}
    chapter_cards, _card_warn = _apply_cards(chapter_cards, _over.get("chapter_cards") or {}, shots)
    shots, _call_warn = _apply_callouts(shots, _over.get("callouts") or {})
    _override_warnings = list(_card_warn) + list(_call_warn)
    from .shorts import build_end_screen as _build_end_screen
    _end_screen = _build_end_screen(duration, chapter)
    # CapCut auto-captions: on by default for NEW builds (video.captions).
    # Old plans (no captions key) render without them unless the user runs
    # `video captions --chapter N`, which adds them to the plan.
    # Captions are timed from the alignment sentences (caption_sentences),
    # never from per-shot estimates: a shot is not a sentence, and
    # per-shot captions gap during filler shots and show a sentence's
    # tail before its words are spoken.
    # Cinematic mode keeps the frame clean like the reference videos: no
    # caption bar, no burned-in captions unless explicitly enabled.
    _captions_on = bool(ccfg.get("captions", False)) if cinematic else bool(vcfg.get("captions", True))
    _captions = []
    if _captions_on:
        _sent = caption_sentences if caption_sentences is not None else sentences
        if _sent:
            _captions = build_captions_from_sentences(_sent, duration)
        if not _captions:
            _captions = build_captions(shots)  # legacy fallback: no sentence timings
        for _s in shots:
            _s["caption_bar"] = True  # dedicated bottom bar, below the cards
    sfx = plan_sfx(shots, chapter_cards)
    return {
        "version": PLAN_VERSION,
        "chapter": chapter,
        "format": vcfg.get("format", "16:9"),
        "fps": int(vcfg.get("fps", 30)),
        "audio": audio,
        "duration": round(duration, 3),
        "sync": mode,
        "sync_report": sync_report,
        "sync_settings": st,
        "alignment_review_queue": (alignment_meta or {}).get("review_queue"),
        # P2: the P0 timing warnings ride with the plan, so status/QA/render
        # all see the same whisper outcome — no silent estimated render.
        "timing_warnings": list((alignment_meta or {}).get("timing_warnings") or []),
        # P3: hook / open-loop status from cues.json (build.py computes it).
        "hook": (alignment_meta or {}).get("hook"),
        # P3: explicit video.music wins, else music/bed.mp3 in the series
        # folder is used when it exists; None renders music-free.
        "music": resolve_music(vcfg, root),
        "music_volume": float(vcfg.get("music_volume", 0.22)),
        "effects_enabled": dict(vcfg.get("effects") or {}),
        "panels_fingerprint": panels_data.get("fingerprint"),
        "timing": timing,
        "crop_stats": designer.crop_stats,
        # P3 editor layer (see editor.py).
        "chapter_cards": chapter_cards,
        # P5: end-screen spec (window + slots + WATCH NEXT). Native
        # YouTube end-screen cards only; never burned into pixels.
        "end_screen": _end_screen,
        "sfx": sfx,
        "captions": _captions,
        "captions_enabled": _captions_on,
        "beat_sync": _beat_info,
        "pacing": {"split_shots": _nsplit, "long_shots_flagged": _pacing_before["long_shots"],
                   "max_shot": pacing_qa(shots)["max_shot"],
                   # Rule 1: max shots = floor(audio_seconds / floor). More shots
                   # than this means the average shot is under the floor.
                   "max_shots": max(0, int(float(duration) // hold_floor)),
                   "min_shot": hold_floor,
                   "mode": "cinematic" if cinematic else "classic"},
        "split_crop_changes": _split_crop_changes,
        "motion_changes": _motion_changes,
        "diversity_changes": _diversity_changes,
        "override_warnings": _override_warnings,
        "reviewed": False,
        "shots": shots,
    }


# ------------------------------------------------------------------ validation & IO


def validate(plan: Dict[str, Any], panel_ids: set) -> List[str]:
    """Problems an editor (human or agent) needs to fix, in plain words."""
    if plan.get("version") != PLAN_VERSION:
        return [f"this plan was made by an older version (needs version {PLAN_VERSION}); rebuild it with --force"]
    errs: List[str] = []
    shots = plan.get("shots")
    if not isinstance(shots, list) or not shots:
        return ["the plan has no shots"]
    duration = float(plan.get("duration") or 0)
    if duration <= 0:
        errs.append("\"duration\" must be a positive number of seconds")
    prev_end = 0.0
    for i, s in enumerate(shots):
        where = f"shot {i + 1} ({s.get('id', '?') if isinstance(s, dict) else '?'})"
        if not isinstance(s, dict):
            errs.append(f"{where} is not an object")
            continue
        try:
            start, end = float(s["start"]), float(s["end"])
        except (KeyError, TypeError, ValueError):
            errs.append(f"{where}: start and end must be numbers")
            continue
        if end - start < 0.2:
            errs.append(f"{where}: must last at least 0.2s (start {start}, end {end})")
        if abs(start - prev_end) > 0.02:
            errs.append(f"{where}: starts at {start} but the previous shot ends at {round(prev_end, 3)}; shots must be back to back")
        if s.get("layout") not in LAYOUTS:
            errs.append(f"{where}: layout must be one of {', '.join(LAYOUTS)}")
        if s.get("background") not in BACKGROUNDS:
            errs.append(f"{where}: background must be one of {', '.join(BACKGROUNDS)}")
        if s.get("exit", "cut") not in EXITS:
            errs.append(f"{where}: exit must be one of {', '.join(EXITS)}")
        if s.get("transition", "cut") not in TRANSITIONS:
            errs.append(f"{where}: transition must be one of {', '.join(TRANSITIONS)}")
        if s.get("grade") not in (None, "subtle"):
            errs.append(f"{where}: grade must be \"subtle\" or absent (off)")
        if s.get("bg_panel") is not None and s["bg_panel"] not in panel_ids:
            errs.append(f"{where}: bg_panel '{s['bg_panel']}' doesn't exist")
        cards = s.get("cards")
        if not isinstance(cards, list) or not cards:
            errs.append(f"{where}: needs at least one card")
            cards = []
        if s.get("layout") == "pair" and len(cards) < 2:
            errs.append(f"{where}: a pair layout needs two cards")
        for k, c in enumerate(cards):
            cw = f"{where} card {k + 1}"
            if not isinstance(c, dict):
                errs.append(f"{cw} is not an object")
                continue
            if c.get("panel") not in panel_ids:
                errs.append(f"{cw}: panel '{c.get('panel')}' doesn't exist (ids are on media/chN/panels_sheet_*.png)")
            crop = c.get("crop", [0, 0, 1, 1])
            try:
                x0, y0, x1, y1 = (float(v) for v in crop)
                if not (0 <= x0 < x1 <= 1 and 0 <= y0 < y1 <= 1) or x1 - x0 < 0.05 or y1 - y0 < 0.05:
                    raise ValueError
            except (TypeError, ValueError):
                errs.append(f"{cw}: crop must be [x0, y0, x1, y1] fractions of the panel, e.g. [0, 0.2, 1, 0.7]")
            try:
                if not 0.0 <= float(c.get("pad_y") or 0.0) <= PAD_LIMIT:
                    raise ValueError
            except (TypeError, ValueError):
                errs.append(f"{cw}: pad_y (blurred padding above and below, in panel heights) must be 0 to {PAD_LIMIT:g}")
            if c.get("enter", "cut") not in ENTRANCES:
                errs.append(f"{cw}: enter must be one of {', '.join(ENTRANCES)}")
            if c.get("motion", "push_in") not in MOTIONS:
                errs.append(f"{cw}: motion must be one of {', '.join(MOTIONS)}")
            try:
                at = float(c.get("at") or 0)
                if at < 0 or (end - start > 0 and at > end - start - 0.2):
                    errs.append(f"{cw}: \"at\" ({at}s after the shot starts) must leave it on screen at least 0.2s")
            except (TypeError, ValueError):
                errs.append(f"{cw}: \"at\" must be seconds after the shot starts")
        from .editor import validate_callout as _validate_callout
        for _co in s.get("callouts") or []:
            for _err in _validate_callout(_co):
                errs.append(f"{where}: callout: {_err}")
        for e in s.get("effects") or []:
            if not isinstance(e, dict) or e.get("type") not in EFFECTS:
                errs.append(f"{where}: effect types are {', '.join(EFFECTS)}")
                continue
            try:
                if not start <= float(e["t"]) <= end:
                    errs.append(f"{where}: {e['type']} at {e['t']}s is outside the shot ({start}-{end}s)")
            except (KeyError, TypeError, ValueError):
                errs.append(f"{where}: every effect needs a time \"t\" in seconds")
        prev_end = end
    for _cc in plan.get("chapter_cards") or []:
        if not isinstance(_cc, dict) or not isinstance(_cc.get("title"), str) or not _cc["title"].strip():
            errs.append("chapter_cards entries need a non-empty string \"title\"")
        elif len(_cc["title"]) > 40:
            errs.append(f"chapter card '{_cc['title'][:20]}...' title must be at most 40 chars")
        else:
            try:
                _t = float(_cc.get("t", -1))
                if not 0 <= _t <= max(duration, 0):
                    errs.append(f"chapter card '{_cc.get('title')}' at {_t}s is outside the video")
            except (TypeError, ValueError):
                errs.append(f"chapter card '{_cc.get('title')}' needs a numeric \"t\"")
    for _sfx in plan.get("sfx") or []:
        if not isinstance(_sfx, dict) or _sfx.get("type") not in tuple(SFX_ALL):
            errs.append(f"sfx entries need one of {', '.join(sorted(SFX_ALL))}")
        else:
            try:
                if not 0 <= float(_sfx.get("t", -1)) <= max(duration, 0):
                    errs.append(f"sfx '{_sfx.get('type')}' at {_sfx.get('t')}s is outside the video")
            except (TypeError, ValueError):
                errs.append(f"sfx '{_sfx.get('type')}' needs a numeric \"t\"")
    for _cerr in validate_captions(plan.get("captions") or [], duration):
        errs.append(f"captions: {_cerr}")
    if duration > 0 and abs(prev_end - duration) > 0.05:
        errs.append(f"the last shot ends at {round(prev_end, 3)}s but the audio is {duration}s long")
    return errs


def save_plan(root: Path, chapter: str, plan: Dict[str, Any]) -> Path:
    path = plan_path(root, chapter)
    atomic_write_text(path, json.dumps(plan, indent=1, ensure_ascii=False))
    return path


def load_plan(root: Path, chapter: str, panel_ids: set | None = None) -> Dict[str, Any]:
    path = plan_path(root, chapter)
    plan = read_agent_json(path, f"Make one first:  video plan --chapter {chapter}")
    if not isinstance(plan, dict):
        raise UserError(f"{path.name} must be a JSON object.")
    if panel_ids is not None:
        errs = validate(plan, panel_ids)
        if errs:
            shown = "\n  - ".join(errs[:12])
            more = f"\n  ... and {len(errs) - 12} more" if len(errs) > 12 else ""
            raise UserError(
                f"{path.name} has problems:\n  - {shown}{more}\n"
                f"Fix them, or rebuild the plan with:  video plan --chapter {chapter} --force"
            )
    return plan
