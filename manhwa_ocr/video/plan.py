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
from .crops import (_panel_maps, content_bands, extend_figure_range, head_of, is_mask_risk,
                    is_system_window, safe_span, tone)
from .cropcache import crop_key, load_crop, store_crop
from .verify import find_pair_safe_shape, pick_card
from .editor import (PAN_DIRECTIONS, SFX_ALL, _inset_card, _inset_crop, build_chapter_cards, extract_callout,
                     pacing_qa, plan_sfx, resolve_beats, resolve_music, snap_cuts_to_beats, split_long_shots)
from .editor import MOTION_WEIGHTS as P3_MOTION_WEIGHTS
from .captions import build_captions, build_captions_from_sentences, validate_captions
from .classify import classify as _classify_sentence
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
ENTRANCE_WEIGHTS = [("cut", 100)]
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
        cur = _inset_card(cur_cards[0], 0.08)
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
        new_frac = None
        for frac in (0.08, 0.16, 0.24, 0.32):
            cand = _inset_crop(cur_crop, frac)
            if cand != cur_crop and _differs(cand):
                new_crop, new_frac = cand, frac
                break
        if new_crop is None:
            continue  # already at the minimum readable crop: nothing more to punch into
        cur = _inset_card(cards[0], new_frac)
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
        elif layout == "wide":
            e = rng.choice(["cut", "cut", "slide_left"])
        else:
            e = _pick(rng, ENTRANCE_WEIGHTS, avoid=self.prev_enter)
        self.prev_enter = e
        return e

    def shot(self, i: int, unit: Dict[str, Any]) -> Dict[str, Any]:
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
            if transition != "cut":
                cards[0]["enter"] = transition_enter(transition)
            if transition == "glitch":
                effects.append({"type": "glitch", "t": round(start, 3)})
                self.last_glitch = start
        background = self.background(i, layout)
        background = (self.fixes["shot"].get(first) or {}).get("background", background)
        shot = {
            "id": f"S{i + 1:03d}", "start": round(start, 3), "end": round(end, 3),
            "layout": layout, "background": background,
            "cards": cards, "exit": "cut", "effects": effects,
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


# ------------------------------------------------------------------ wide mode

# Wide mode ("video.mode: wide") is classic editing with wide panels: every
# card touches the top and bottom of the frame and is at least 6/16 of the
# frame wide (pairs exactly 8/16); no thin slivers ever. The window CONTAINS
# the classic focal crop (the head-aware, bubble-avoiding crop the classic
# planner already computed), grown by headroom on each end, top-anchored to
# that crop's own top, then nudged so its edges never land mid-bubble or
# mid-caption (crops.content_bands / crops.safe_span). Centring the window
# on the subject was tried first and rejected: the overflow split both ways
# and landed edges mid-bubble, mid-horns (wide_mode_v3_all_issues.md).
WIDE_MIN_ASPECT = 0.667    # 6/16 of the frame at full height (720px @1080p)
WIDE_HEADROOM = 0.08       # window grows this much past the focal crop per end
WIDE_CAPTION_ZONE = 0.12   # bottom share of the window captions overlay (no bar in wide mode)
WIDE_EDGE_MARGIN = 0.06    # whole-panel: text within this of an edge needs zoom protection


def wide_crop(pw: float, ph: float, box: List[float], min_win_h: float,
             pair_aspect: float | None = None, img: Image.Image | None = None
             ) -> Tuple[List[float], Dict[str, Any]]:
    """A wide-mode window over one panel, in panel fractions, plus diagnostics
    {"whole", "edge_text", "caption_risk"} (all bool).

    `box` is the classic focal crop for the panel. `img`, when given, lets
    the window edges avoid landing mid-bubble/mid-caption/mid-SFX and warns
    about a whole panel's own edge text (zoom would clip it) or a window
    whose bottom share collides with the overlaid captions.
    """
    try:
        bx0, by0, bx1, by1 = (float(v) for v in box[:4])
    except (TypeError, ValueError, IndexError):
        bx0, by0, bx1, by1 = 0.0, 0.0, 1.0, 1.0
    bx0 = max(0.0, min(1.0, bx0)); by0 = max(0.0, min(1.0, by0))
    bx1 = max(bx0, min(1.0, bx1)); by1 = max(by0, min(1.0, by1))
    panel_aspect = pw / max(ph, 1e-9)
    info: Dict[str, Any] = {"whole": False, "edge_text": False, "caption_risk": False, "figure_risk": False}
    # A panel whose own aspect already fits the 6/16-16/9 single-card range
    # is shown whole: the full figure stays visible, nothing is cropped, and
    # it still touches the top AND bottom of the frame at this aspect. A
    # panel WIDER than 16/9 is never "shown whole" with top/bottom fill
    # instead -- every card touches both top and bottom, no exception --
    # so it falls through to the windowed branch below, which already caps
    # the width to the 16/9 shape (cropping the sides, centred) while
    # keeping the full panel height.
    if pair_aspect is None and WIDE_MIN_ASPECT <= panel_aspect <= 1.777:
        info["whole"] = True
        if img is not None:
            bands = (content_bands(img, axis="y", exclude=[(by0, by1)])
                    + content_bands(img, axis="x", exclude=[(bx0, bx1)]))
            info["edge_text"] = any(b0 <= WIDE_EDGE_MARGIN or b1 >= 1 - WIDE_EDGE_MARGIN for b0, b1 in bands)
        return [0.0, 0.0, 1.0, 1.0], info
    subj_w, subj_h = (bx1 - bx0) * pw, (by1 - by0) * ph
    grow = 1.0 + 2.0 * WIDE_HEADROOM
    if pair_aspect is not None:
        win_h = min(ph, max(subj_h * grow, min(min_win_h, ph)))
        win_w = win_h * pair_aspect
        if win_w > pw:                       # panel narrower than the window
            win_w, win_h = pw, pw / pair_aspect
    else:
        win_h = min(ph, pw / WIDE_MIN_ASPECT, max(subj_h * grow, min(min_win_h, ph)))
        win_w = min(pw, win_h * 1.777)
        if win_w / max(win_h, 1e-9) < WIDE_MIN_ASPECT:
            win_h = win_w / WIDE_MIN_ASPECT

    # Vertical: top-anchored to the focal crop's own top, clamped so the
    # window still contains the whole subject and stays inside the panel.
    lo_y = max(0.0, by1 * ph - win_h)
    hi_y = min(by0 * ph, max(0.0, ph - win_h))
    if hi_y < lo_y:
        # The subject is taller than any window this layout/upscale cap
        # allows: it cannot be fully contained. Flagged, not silently cut --
        # see plan["wide_figure_risk"] / qa["wide_figure_risk"].
        info["figure_risk"] = True
        lo_y, hi_y = 0.0, max(0.0, ph - win_h)
    y0 = max(lo_y, min(hi_y, by0 * ph))
    # Horizontal: centred on the focal crop, same containment clamp.
    cx = (bx0 + bx1) / 2.0 * pw
    lo_x = max(0.0, bx1 * pw - win_w)
    hi_x = min(bx0 * pw, max(0.0, pw - win_w))
    if hi_x < lo_x:
        lo_x, hi_x = 0.0, max(0.0, pw - win_w)
    x0 = max(lo_x, min(hi_x, cx - win_w / 2.0))

    if img is not None:
        yb = content_bands(img, axis="y", exclude=[(by0, by1)])
        xb = content_bands(img, axis="x", exclude=[(bx0, bx1)])
        y0 = safe_span(ph, win_h, lo_y, hi_y, yb, y0)
        x0 = safe_span(pw, win_w, lo_x, hi_x, xb, x0)

        # Captions overlay the art in wide mode: a band inside the window's
        # bottom share collides with the caption zone. First try pushing
        # the window down (more panel below the text); a single (not a
        # pair -- its aspect is fixed) may also grow taller, which shrinks
        # the band's share of the window. If neither clears it, flag it.
        def _collides(y0v: float, h: float) -> bool:
            zone = h * WIDE_CAPTION_ZONE
            return any(b0 * ph < y0v + h and b1 * ph > y0v + h - zone for b0, b1 in yb)

        if _collides(y0, win_h):
            pushed = safe_span(ph, win_h, y0, hi_y, yb, hi_y)
            if not _collides(pushed, win_h):
                y0 = pushed
            elif pair_aspect is None:
                max_h = min(ph, win_w / WIDE_MIN_ASPECT)
                step = max(ph * 0.02, 4.0)
                grown_h, found = win_h, False
                while grown_h < max_h - 1e-6:
                    grown_h = min(max_h, grown_h + step)
                    grown_y0 = max(0.0, min(by0 * ph, ph - grown_h))
                    if not _collides(grown_y0, grown_h):
                        win_h, y0, found = grown_h, grown_y0, True
                        break
                if not found:
                    info["caption_risk"] = True
            else:
                info["caption_risk"] = True

    return [round(x0 / pw, 4), round(y0 / ph, 4), round((x0 + win_w) / pw, 4),
           round((y0 + win_h) / ph, 4)], info


def _wide_style_card(c: Dict[str, Any], designer: "_Designer", pw: float, ph: float,
                     min_win_h: float, pair_aspect: float | None) -> Dict[str, bool]:
    """Rewrite one card's crop/motion/sizing fields in place for the wide-mode
    treatment (full frame height, edge-safe window, halved/capped Ken Burns,
    dead pans swapped for zooms). Shared by _wide_pass (every card) and
    _adaptive_pass (only the cards a naturally wide/landscape panel picks
    for this treatment), so the two never drift out of sync.

    Returns {"caption_risk", "figure_risk", "reviewer_focal_ignored"} (bool)
    for THIS card -- the caller appends its panel id to its own risk lists.
    """
    pid = c.get("panel")
    flags = {"caption_risk": False, "figure_risk": False, "reviewer_focal_ignored": False}
    img = designer.img(pid) if pid in designer.by_id else None
    box = list(c.get("crop") or [0, 0, 1, 1])
    if img is not None:
        # The classic focal crop can itself exclude the figure (a narrow
        # landscape/portrait shape outscores a window that includes the
        # head, or the pick lands on a different detail) -- it is still
        # flagged head_cut, but classic mode's own card just shows that
        # strip. Wide-style windows must not inherit that miss: grow the
        # subject to also cover the detected head AND the rest of the
        # figure's own body (wings, tail, legs) connected to it, before
        # anchoring to it. A head/face detector only answers "where is the
        # head", never "where does the figure end" -- that is a separate,
        # full-body cutoff a confident head alone cannot catch (see
        # wide_mode_v3_all_issues.md: dragons cut mid-body even where the
        # head detector was right).
        head = head_of(_panel_maps(img, designer.map_options))
        if (head.get("figure") and head.get("top") is not None
                and head.get("bottom") is not None
                and pid not in (designer.fixes.get("crop") or {})):
            # Cap the extension at exactly the span this panel's own legal
            # window can hold (pre-grow), instead of a flat default: an
            # all-or-nothing guard against an over-tall "figure" used to
            # discard the WHOLE union on a narrow panel (pw/WIDE_MIN_ASPECT
            # too small for even a 75%-of-panel span), reverting to the
            # un-extended focal crop and reintroducing the same full-body
            # cutoff this hardening exists to fix. A legal-but-partial
            # extension, centred on the figure, is always better than none.
            grow = 1.0 + 2.0 * WIDE_HEADROOM
            max_h_px = ph if pair_aspect is not None else min(ph, pw / WIDE_MIN_ASPECT)
            max_span = max_h_px / grow / ph
            top, bottom = extend_figure_range(img, float(head["top"]), float(head["bottom"]),
                                              max_span=max_span)
            box[1] = min(box[1], top)
            box[3] = max(box[3], bottom)
    crop, info = wide_crop(pw, ph, box, min_win_h, pair_aspect, img=img)
    c["crop"] = crop
    flags["caption_risk"] = bool(info.get("caption_risk"))
    flags["figure_risk"] = bool(info.get("figure_risk"))
    has_reviewer_crop = pid in (designer.fixes.get("crop") or {})
    if has_reviewer_crop and (info.get("whole") or info.get("figure_risk")):
        # The reviewer's own focal is either discarded (panel shown whole)
        # or taller than any window this panel's width/upscale cap can
        # fully contain: wide_crop still shows something (top-anchored
        # fallback), but not the exact box the reviewer set.
        flags["reviewer_focal_ignored"] = True
    # Whole-panel windows have zero margin: the Ken Burns zoom would clip
    # text sitting at the panel's own edge. Cut the zoom amplitude further
    # for just this card instead of the mode-wide 0.5 factor.
    c["wide_zoom_scale"] = 0.12 if info.get("edge_text") else 0.5
    # A pan across a window that already spans the panel's full width (or
    # full height) has nowhere to travel: the card renders as a frozen
    # still while only the captions change, which reads as the picture
    # "refreshing" every couple of seconds. Swap dead pans for a zoom,
    # which always moves.
    _m = c.get("motion")
    if _m in ("pan_left", "pan_right") and crop[2] - crop[0] >= 0.999:
        c["motion"] = "push_in" if _m == "pan_right" else "pull_out"
    elif _m in ("pan_up", "pan_down") and crop[3] - crop[1] >= 0.999:
        c["motion"] = "push_in" if _m == "pan_down" else "pull_out"
    c["pad_y"] = 0.0
    c["wide_display"] = True
    return flags


def _wide_pass(shots: List[Dict[str, Any]], designer: "_Designer", fmt: Format,
               wcfg: Dict[str, Any]) -> Dict[str, List[str]]:
    """Rewrite a classic plan for wide mode: single + pair layouts only
    (inset/classic-wide become full-height singles), crops become wide
    focal windows, caption_bar is off so panels touch the bottom edge and
    captions overlay the art. Runs after the classic plan is fully built
    (punch-ins, pairs, splits already decided) so every card -- first
    showing, punch-in, each side of a pair -- gets the same edge audit.

    Returns {"caption_risk": [...], "figure_risk": [...],
    "reviewer_focal_ignored": [...]} -- panel ids flagged with a caption/
    text collision, a figure too tall for any window this layout/upscale
    cap allows to fully contain, or a fixes.json focal crop that wide mode
    could not use as-is (the panel was shown whole because its own aspect
    is >= 6/16, or the focal itself doesn't fit and was re-anchored) --
    for an honest report rather than a silent miss (plan["wide_caption_risk"]
    / plan["wide_figure_risk"] / plan["wide_reviewer_focal_ignored"]).
    """
    if fmt.width <= fmt.height:
        return {"caption_risk": [], "figure_risk": [], "reviewer_focal_ignored": []}  # portrait formats untouched
    min_win_h = fmt.height / max(0.5, float((wcfg or {}).get("max_upscale", 1.5)))
    pair_aspect = (fmt.width / 2.0) / fmt.height
    caption_risk: List[str] = []
    figure_risk: List[str] = []
    reviewer_focal_ignored: List[str] = []
    for s in shots:
        s["wide"] = True
        s["caption_bar"] = False
        cards = s.get("cards") or []
        pair = s.get("layout") == "pair" and len(cards) >= 2
        if not pair:
            s["layout"] = "single"
            cards = cards[:1]
        for c in cards:
            pid = c.get("panel")
            p = designer.by_id.get(pid) or {}
            pw, ph = float(p.get("width") or 0), float(p.get("height") or 0)
            if pw > 0 and ph > 0:
                flags = _wide_style_card(c, designer, pw, ph, min_win_h, pair_aspect if pair else None)
                if flags["caption_risk"] and pid not in caption_risk:
                    caption_risk.append(pid)
                if flags["figure_risk"] and pid not in figure_risk:
                    figure_risk.append(pid)
                if flags["reviewer_focal_ignored"] and pid not in reviewer_focal_ignored:
                    reviewer_focal_ignored.append(pid)
            else:
                c["pad_y"] = 0.0
                c["wide_display"] = True
        s["cards"] = cards
    return {"caption_risk": caption_risk, "figure_risk": figure_risk,
           "reviewer_focal_ignored": reviewer_focal_ignored}


def _reposition_for_figure(box: List[float], fig_top: float, fig_bottom: float) -> List[float]:
    """Slide a FIXED-size crop box (same width/height -- it must stay one of
    the classic allowed shapes) vertically within the panel so it best
    contains [fig_top, fig_bottom] (panel fractions), instead of wherever
    the classic bubble-avoidance pick happened to land it. Never changes
    the box's width or height, only its position; when the box is shorter
    than the figure it cannot fully contain it (same honest limit as
    wide_crop's figure_risk), so it centres on the figure as a best effort.
    """
    x0, y0, x1, y1 = box
    h = y1 - y0
    if h <= 0:
        return box
    lo = max(0.0, fig_bottom - h)
    hi = min(fig_top, 1.0 - h)
    if hi < lo:
        mid = (fig_top + fig_bottom) / 2.0
        y0n = max(0.0, min(1.0 - h, mid - h / 2.0))
    else:
        y0n = max(lo, min(hi, fig_top))
    return [x0, round(y0n, 4), x1, round(y0n + h, 4)]


def _adaptive_pass(shots: List[Dict[str, Any]], designer: "_Designer", fmt: Format,
                   acfg: Dict[str, Any], wcfg: Dict[str, Any]) -> Dict[str, List[str]]:
    """video.mode: adaptive -- per shot (not the whole video), show the panel
    in whichever treatment actually suits its own shape:

    - a naturally wide/landscape panel (its own aspect >= video.adaptive.
      wide_threshold) gets the SAME full-frame-height, edge-safe wide-mode
      window as video.mode: wide (via _wide_style_card -- the two modes
      share one implementation so they can never drift apart);
    - a narrower panel keeps its classic crop/shape exactly as the planner
      already chose, but drops the equal-area shrink (card.wide_fill) that
      forces every shape onto the same on-screen size -- the "stale, small
      panel" the equal-area rule otherwise produces regardless of how much
      visual weight a panel's own content actually carries.

    Pairs are never wide-style (classic pairing already excludes landscape
    panels from pairing -- see _pair_up's `wide()` guard), so a pair shot's
    two cards are always the classic-style branch.

    Captions always overlay the art here (never a dedicated bar), the same
    way for every shot regardless of which treatment that shot's own card
    took -- a video that flips between a caption bar and overlaid captions
    shot to shot is exactly the kind of visual inconsistency this mode
    exists to avoid.

    Returns the same {"caption_risk", "figure_risk", "reviewer_focal_ignored"}
    shape as _wide_pass (plan["wide_caption_risk"] / etc. are reused).
    """
    if fmt.width <= fmt.height:
        return {"caption_risk": [], "figure_risk": [], "reviewer_focal_ignored": []}
    threshold = float((acfg or {}).get("wide_threshold", 1.333))
    min_win_h = fmt.height / max(0.5, float((wcfg or {}).get("max_upscale", 1.5)))
    caption_risk: List[str] = []
    figure_risk: List[str] = []
    reviewer_focal_ignored: List[str] = []
    for s in shots:
        s["caption_bar"] = False
        cards = s.get("cards") or []
        pair = s.get("layout") == "pair" and len(cards) >= 2
        any_wide = False
        for c in cards:
            pid = c.get("panel")
            p = designer.by_id.get(pid) or {}
            pw, ph = float(p.get("width") or 0), float(p.get("height") or 0)
            if pw <= 0 or ph <= 0:
                continue
            if not pair and (pw / ph) >= threshold:
                flags = _wide_style_card(c, designer, pw, ph, min_win_h, None)
                if flags["caption_risk"] and pid not in caption_risk:
                    caption_risk.append(pid)
                if flags["figure_risk"] and pid not in figure_risk:
                    figure_risk.append(pid)
                if flags["reviewer_focal_ignored"] and pid not in reviewer_focal_ignored:
                    reviewer_focal_ignored.append(pid)
                any_wide = True
            else:
                # Classic-style: the card is bigger on screen (wide_fill, no
                # equal-area shrink) than a classic build ever showed it, so
                # a crop that excludes part of the figure -- the same thing
                # _wide_style_card hardens against for wide-aspect panels --
                # is far more visible here too. Reposition the SAME fixed
                # shape (never resized, so it stays an allowed classic
                # shape) to best contain the detected figure, instead of
                # leaving it wherever classic's bubble-avoidance-only pick
                # happened to land.
                img = designer.img(pid) if pid in designer.by_id else None
                box = list(c.get("crop") or [0, 0, 1, 1])
                if img is not None and pid not in (designer.fixes.get("crop") or {}):
                    head = head_of(_panel_maps(img, designer.map_options))
                    if head.get("figure") and head.get("top") is not None and head.get("bottom") is not None:
                        # The crop box's height is FIXED (reposition only
                        # slides it, never resizes -- it must stay an
                        # allowed classic shape), so that height is exactly
                        # this call's legal budget: a figure taller than it
                        # already gets _reposition_for_figure's own
                        # best-effort centring, not a mid-way geometry mess.
                        top, bottom = extend_figure_range(img, float(head["top"]), float(head["bottom"]),
                                                          max_span=box[3] - box[1])
                        new_box = _reposition_for_figure(box, top, bottom)
                        yb = content_bands(img, axis="y", exclude=[(top, bottom)])
                        h = new_box[3] - new_box[1]
                        y0 = safe_span(1.0, h, 0.0, 1.0 - h, yb, new_box[1])
                        box = [new_box[0], round(y0, 4), new_box[2], round(y0 + h, 4)]
                c["crop"] = box
                c["wide_fill"] = True
        s["wide"] = any_wide
    return {"caption_risk": caption_risk, "figure_risk": figure_risk,
           "reviewer_focal_ignored": reviewer_focal_ignored}


# ------------------------------------------------------------------ panel-fit pass
#
# Panel-fit mode (video.mode: panelfit) — the MEASURED reference style
# (~/workspace/your_files/reference-video-measured-spec.md; 85 shots of
# the channel's own video, measured frame by frame). No cards, no
# cropping of ordinary panels: every shot's panel is shown WHOLE at its
# native aspect ratio, sharp and centred, over the same panel
# cover-scaled and heavily blurred (50px) at FULL brightness — the
# text descriptions' "60% opacity" was measured to be wrong, and the
# measurements govern (user arbitration 2026-10-23: "copy the video
# exactly — no captions, no shake/flash").
#
# The measured grammar, as coded below:
#   * aspect (w/h) >= PF_FILL_ASPECT (~1.6): the panel fills the frame
#     full-bleed (3/3 measured cases; blur bands are never used).
#   * narrower panels: height-fit pillars over the blur. Motion is a
#     gentle vertical DRIFT (total travel 5-13% of frame height at a
#     constant ~6.8%/s of frame height, direction alternating) for
#     pillars under PF_DRIFT_ASPECT_MAX, else a zoom-IN at a constant
#     RATE (+5.0%/s; +8.8%/s on impact shots) — never a fixed total,
#     never a zoom-out, never a full strip traversal (the old width-fit
#     scroll is gone: their tallest pillar, aspect 0.31, is height-fit
#     and merely drifts).
#   * NO impact shake, NO white flash (measured: neither exists in the
#     reference; impacts are fast zooms).
#   * NO burned-in captions in this mode (measured: none; PF_CAPTIONS).
#   * Rhythm: this mode's shot floor is PF_MIN_SHOT (0.3s, the measured
#     minimum) and sentences over PF_SPLIT_ABOVE split in two (the
#     reference's median shot is 1.97s).
#   * Timeline cues in the narration become split-flap TITLE CARDS
#     (titlecards.py + title_card_overlays below).
# Every parameter is a named constant, pinned to the config defaults
# (video.panelfit.*) and to the renderer's mirrors (frames.PFR_*) by
# tests; the values must never live only in an agent's head.
#
# Assets reach this pass already white-space-eradicated (Rule 6:
# panels.trim_v2 at panel build — ink-bbox trim + gap slicing), so the
# composed frame contains no white margins in either layer.
#
# Parallax is NOT here: the detector stack yields boxes, not character
# segmentations; deferred to build B11 of the movie plan.

PF_BG_BLUR = 50            # background Gaussian blur radius, px at 1080p
PF_BG_OPACITY = 1.0        # background layer opacity over black: FULL brightness
PF_FILL_ASPECT = 1.6       # panel aspect (w/h) at/above which it fills the frame
PF_DRIFT_ASPECT_MAX = 0.72  # pillars narrower than this drift; wider ones zoom
PF_DRIFT_SPEED = 0.0658    # drift speed, frame-heights per second. The reference
                           # fixes SPEED and lets travel vary (B1d F2): 3.7% of
                           # frame WIDTH per second at 16:9 ≈ 71 px/s at 1080p
                           # (measured band 3.2-4.3% width/s). B1c instead fixed
                           # travel at the cap and derived speed, which measured
                           # slow (2.2-2.9% width/s) on the sample.
PF_DRIFT_MIN_TRAVEL = 0.05  # span travel floor, share of frame height (centering/
                           # oversize for very short spans; the motion itself
                           # always runs at PF_DRIFT_SPEED and may not finish it)
PF_DRIFT_MAX_TRAVEL = 0.13  # span travel cap, share of frame height: at the cap
                           # the panel HOLDS position — never reverses, never
                           # slows down to stretch the travel over the span
PF_DRIFT_HEADROOM = 0.03   # fg oversize beyond the travel (edges never show)
PF_ZOOM_RATE = 0.05        # zoom-in rate per second (measured band 3.6-6.5%/s)
PF_ZOOM_RATE_IMPACT = 0.088  # impact zoom-in rate per second (measured, clash shot)
PF_ZOOM_MAX_TOTAL = 0.50   # total zoom cap: at +50% the scale HOLDS (measured
                           # totals top out at +46%; the cap binds only on
                           # shots longer than the reference ever runs)
PF_MAX_UPSCALE = 2.4       # quality guard: never upscale a panel past this
PF_STILL_LIMIT = 0.5       # spec: no image still for longer than this, seconds
PF_MIN_SHOT = 0.3          # this mode's shot floor (measured minimum shot)
PF_SPLIT_ABOVE = 3.0       # sentences longer than this split into two shots
                           # (B1d F7: 3.5 -> 3.0, moving the median shot toward
                           # the reference's 1.97s; the residual gap is the
                           # script's sentence lengths, not the planner)
PF_SPLIT_MIN_PIECE = 1.2   # smallest piece such a split may produce
PF_CAPTIONS = False        # no burned-in captions in this mode (measured: none)
PF_TITLE_CARDS = True      # timeline cues become split-flap title cards
# Analysis fixes (user's second-by-second analysis, 2026-10-23):
PF_SHADOW_DY = 20          # foreground drop shadow: Y offset, px at 1080p
PF_SHADOW_BLUR = 10        # ... blur radius, px at 1080p
PF_SHADOW_OPACITY = 0.40   # ... black at 40%; contrast comes from this
                           # shadow — the background is NEVER dimmed
PF_GUTTER_SEGMENTS = True  # shots split at their panel's gutter segments
                           # (analysis fix 1; segments recorded at panel
                           # build as panels.gutter_segments)
PF_TTS_GAP = 0.11          # the mode's recommended tts.gap_seconds. MEASURED basis
                           # (B1d F4): the 0.20 setting rendered as 0.296s median
                           # silence in the B1c sample — per-sentence TTS edge
                           # padding adds ≈ +0.09s — so 0.11 lands the MEASURED
                           # gap at ≈ 0.20s, on the reference's 0.214s median.
PF_ASPECT_TOL = 0.01       # log-aspect distance under which a panel counts as exactly canvas-shaped
PF_FIT_TOL = 1.0           # px slack when checking a fitted foreground stays inside the frame
# The coded fallback's impact verbs: the same hit words the SFX planner
# (editor.plan_sfx) keys "hit" sounds on. In this mode an impact only
# selects the fast zoom rate — there is no shake/flash to trigger.
PF_IMPACT_WORDS = ("slams", "crashes", "explodes", "strikes", "smashes", "blasts", "shatters")


def panelfit_drift_travel(dur: float, pcfg: Dict[str, Any] | None = None) -> float:
    """Total vertical travel of a drift SPAN, as a share of frame
    height: the pinned constant speed times the span's duration,
    clamped to the measured 5-13% band. `dur` is the whole span the
    motion is planned over (a sentence, across its sub-shots), never a
    single sub-shot: speed is the invariant, travel is derived (F2)."""
    pcfg = pcfg or {}
    speed = float(pcfg.get("drift_speed", PF_DRIFT_SPEED))
    lo = float(pcfg.get("drift_min_travel", PF_DRIFT_MIN_TRAVEL))
    hi = float(pcfg.get("drift_max_travel", PF_DRIFT_MAX_TRAVEL))
    return round(min(hi, max(lo, speed * max(0.0, float(dur)))), 4)


def panelfit_drift_offset(travel: float, tau: float, direction: str,
                          pcfg: Dict[str, Any] | None = None) -> float:
    """Signed foreground offset (share of frame height, down-positive)
    at `tau` seconds into a drift span whose total travel is `travel`:
    the span starts at -travel/2 (down) / +travel/2 (up), moves at the
    pinned SPEED, and HOLDS at the far edge once the travel is spent —
    it never reverses and never slows to stretch the travel (F2). The
    planner slices spans with this function and the renderer replays
    it, so both agree on the position at every seam."""
    pcfg = pcfg or {}
    speed = float(pcfg.get("drift_speed", PF_DRIFT_SPEED))
    pos = -float(travel) / 2.0 + min(speed * max(0.0, float(tau)), float(travel))
    return pos if direction == "down" else -pos


def panelfit_fit(pw: float, ph: float, fmt: Format, *, dur: float = 2.0,
                 pcfg: Dict[str, Any] | None = None) -> Tuple[str, float, float, float]:
    """(kind, base_w, base_h, scale) for one panel on the canvas.

    "fill": aspect >= PF_FILL_ASPECT — the panel cover-fills the whole
    frame (full-bleed; cropped by the frame, never letterboxed). Only
    when the cover scale fits under the upscale cap: a wide-but-tiny
    panel that cannot actually fill at the cap falls through to the
    capped pillar fit instead (over blur, smaller, never stretched).
    "drift": a narrow pillar (aspect < PF_DRIFT_ASPECT_MAX), height-fit
    and oversized by (travel + headroom) so its gentle vertical drift
    never exposes a panel edge.
    "zoom": a wider pillar, height-fit exactly; it zoom-ins by rate.
    All fits preserve the panel's aspect exactly and cap the scale at
    PF_MAX_UPSCALE (a capped pillar sits smaller; never a stretch).
    """
    pcfg = pcfg or {}
    cap = max(0.1, float(pcfg.get("max_upscale", PF_MAX_UPSCALE)))
    fill_aspect = float(pcfg.get("fill_aspect", PF_FILL_ASPECT))
    drift_max = float(pcfg.get("drift_aspect_max", PF_DRIFT_ASPECT_MAX))
    pw, ph = max(1.0, float(pw)), max(1.0, float(ph))
    aspect = pw / ph
    if aspect >= fill_aspect:
        s_cover = max(fmt.width / pw, fmt.height / ph)
        if s_cover <= cap:
            return "fill", pw * s_cover, ph * s_cover, s_cover
    s = min(fmt.height / ph, fmt.width / pw, cap)
    if aspect < drift_max:
        over = 1.0 + panelfit_drift_travel(dur, pcfg) \
            + float(pcfg.get("drift_headroom", PF_DRIFT_HEADROOM))
        return "drift", pw * s * over, ph * s * over, s * over
    return "zoom", pw * s, ph * s, s


def panelfit_spec(pw: float, ph: float, motion: str, fmt: Format,
                  pcfg: Dict[str, Any] | None, *, dur: float = 2.0,
                  impact: bool = False, span_dur: float | None = None,
                  elapsed: float = 0.0, direction: str | None = None) -> Dict[str, Any]:
    """The per-card composition spec the renderer consumes.

    `motion` (the classic designer's pick) no longer steers this mode:
    routing is by aspect + impact only, zoom is always IN (the reference
    never zooms out, and pull_out does not reverse), and drift
    direction is assigned by the pass (alternating per sentence, as
    measured).

    Motion is planned over the whole SPAN a sentence occupies
    (`span_dur`, defaulting to this shot's own `dur`) and sliced per
    sub-shot: `elapsed` is how much of the span precedes this sub-shot.
    A zoom slice carries `zoom_start` (the scale the span has reached
    when the sub-shot opens); a drift slice carries `drift_from` /
    `drift_to` / `drift_rate` (the span's signed offsets and its
    constant speed). Sub-shot 2 therefore opens exactly where sub-shot
    1 ended — no reset, never a reversal (B1d F1).
    """
    pcfg = pcfg or {}
    dur = max(0.0, float(dur))
    span = max(dur, float(span_dur)) if span_dur is not None else dur
    elapsed = min(max(0.0, float(elapsed)), span)
    kind, bw, bh, _s = panelfit_fit(pw, ph, fmt, dur=span, pcfg=pcfg)
    rate = 0.0
    if kind in ("zoom", "fill"):
        rate = float(pcfg.get("zoom_rate_impact" if impact else "zoom_rate",
                              PF_ZOOM_RATE_IMPACT if impact else PF_ZOOM_RATE))
    bg = True
    if pw > 0 and ph > 0:
        bg = abs(math.log((pw / ph) / (fmt.width / fmt.height))) > PF_ASPECT_TOL
    zoom_cap = 1.0 + float(pcfg.get("zoom_max_total", PF_ZOOM_MAX_TOTAL))
    spec: Dict[str, Any] = {"kind": kind, "dir": "down" if kind == "drift" else "in",
                            "bg": bg, "base_w": round(bw, 2), "base_h": round(bh, 2),
                            "zoom_rate": round(rate, 4),
                            "zoom_end": round(min(1.0 + rate * span, zoom_cap), 4),
                            "drift_frac": 0.0, "impact": bool(impact)}
    if kind in ("zoom", "fill"):
        spec["zoom_start"] = round(min(1.0 + rate * elapsed, zoom_cap), 4)
    elif kind == "drift":
        d = direction if direction in ("down", "up") else "down"
        travel = panelfit_drift_travel(span, pcfg)
        off0 = panelfit_drift_offset(travel, elapsed, d, pcfg)
        off1 = panelfit_drift_offset(travel, elapsed + dur, d, pcfg)
        speed = float(pcfg.get("drift_speed", PF_DRIFT_SPEED))
        spec.update({"dir": d,
                     "drift_from": round(off0, 4),
                     "drift_to": round(panelfit_drift_offset(travel, span, d, pcfg), 4),
                     "drift_rate": round(speed if d == "down" else -speed, 4),
                     # this sub-shot's own signed travel (0.0 on a cap-hold slice)
                     "drift_frac": round(off1 - off0, 4)})
    return spec


def panelfit_advance_slice(spec: Dict[str, Any], cut_in: float) -> Dict[str, Any]:
    """Advance a panelfit span slice by `cut_in` seconds — a clip that
    starts mid-shot (build._clip_plan) skips the shot's head, so the
    slice must open at the position the SPAN had reached by then, not
    at the slice's own start. Without this, a clip starting inside a
    split sentence replays the span from its beginning on the first
    piece and jumps at the seam (B1d F1, found on the proof sample:
    a 65px position jump at the first seam of a mid-span clip).
    Legacy specs (no slice keys) are returned unchanged."""
    sp = dict(spec or {})
    cut = max(0.0, float(cut_in))
    if cut == 0.0:
        return sp
    if sp.get("kind") == "drift" and sp.get("drift_from") is not None:
        frm, to = float(sp["drift_from"]), float(sp["drift_to"])
        rate = float(sp["drift_rate"])
        new = frm + rate * cut
        new = min(new, to) if rate > 0 else max(new, to)
        sp["drift_from"] = round(new, 4)
        sp["drift_frac"] = round(float(sp["drift_frac"]) - (new - frm), 4)
    elif sp.get("kind") in ("zoom", "fill") and "zoom_start" in sp:
        sp["zoom_start"] = round(min(float(sp["zoom_start"])
                                     + float(sp["zoom_rate"]) * cut,
                                     float(sp["zoom_end"])), 4)
    return sp


def panelfit_is_impact(shot: Dict[str, Any]) -> Tuple[bool, str | None]:
    """(is_impact, rule) for one shot. Flags come from the plan's
    existing beat markers first (shot["effects"], built from the
    {impact}/{reveal} cues — a shake or flash effect marks the beat);
    fallback (coded): the shot's sentence classifies as VISUAL_ACTION
    and carries an impact verb. In this mode the ONLY consequence is
    the fast zoom rate (PF_ZOOM_RATE_IMPACT) — no shake, no flash."""
    for e in shot.get("effects") or []:
        if isinstance(e, dict) and e.get("type") in ("shake", "flash"):
            return True, "marker"
    sentence = str(shot.get("sentence") or "")
    if sentence:
        kind, _why = _classify_sentence(sentence)
        low = sentence.lower()
        if kind == "VISUAL_ACTION" and any(w in low for w in PF_IMPACT_WORDS):
            return True, "fallback"
    return False, None


def _panelfit_pass(shots: List[Dict[str, Any]], designer: "_Designer", fmt: Format,
                   pcfg: Dict[str, Any]) -> Dict[str, Any]:
    """Rewrite classic shots into panel-fit shots (landscape only, like
    the wide/adaptive passes; portrait formats stay classic).

    Motion is planned per SENTENCE SPAN, not per shot (B1d F1): a
    maximal run of consecutive shots carrying the same sentence on the
    same panel(s) — the pieces a long sentence split into — shares ONE
    motion, sliced per piece (see panelfit_spec's span_dur/elapsed), so
    the position is continuous at every seam and the direction can
    never reverse inside a sentence. Drift direction alternates per
    sentence span, as measured — except that a panel reused by the
    immediately following sentence keeps the direction it already has.

    Returns {"static", "hold", "over_cap", "impact"} — shot ids whose
    whole span carries no motion (none by construction; computed
    honestly from the specs, not asserted), shot ids that sit entirely
    in a travel-cap hold (the span's motion finished before they open —
    the F2 hold, reported, never hidden), panel ids whose uncapped fit
    would exceed PF_MAX_UPSCALE (their foreground is capped, so it sits
    smaller — an honest quality note, never a stretch), and the impact
    report (which shots earned the fast zoom rate, and by which rule).
    """
    report: Dict[str, Any] = {"static": [], "hold": [], "over_cap": [], "impact": [],
                              "segments": []}
    if fmt.width <= fmt.height:
        return report
    pcfg = pcfg or {}
    cap = float(pcfg.get("max_upscale", PF_MAX_UPSCALE))
    fill_aspect = float(pcfg.get("fill_aspect", PF_FILL_ASPECT))
    for s in shots:
        s["panelfit"] = True
        s["caption_bar"] = False  # this mode burns no captions at all
        cards = s.get("cards") or []
        pair = s.get("layout") == "pair" and len(cards) >= 2
        if not pair:
            s["layout"] = "single"
            cards = cards[:1]
        s["cards"] = cards
    # Analysis fix 1 (the user's "double panel" fix): a single-card
    # shot whose panel holds stacked images across an interior gutter
    # (panels.gutter_segments, recorded at panel build under trim_v2)
    # splits into one sub-shot per segment, sequenced inside the shot's
    # own time — feather first, then the "BUT..." panel; the detonator
    # press, then the explosion. Pieces divide the shot's duration in
    # proportion to their heights, never below the mode's PF_MIN_SHOT
    # floor (the shortfall is borrowed from the largest piece); a shot
    # too short to give every segment its floor stays whole. Pair
    # shots are untouched: their two cards are already separate
    # assets. Ids are renumbered afterwards (the title-card pass used
    # to renumber too; everything downstream reads the final ids).
    if bool(pcfg.get("gutter_segments", PF_GUTTER_SEGMENTS)):
        expanded: List[Dict[str, Any]] = []
        for s in shots:
            cards = s.get("cards") or []
            segs: List[Tuple[float, float]] = []
            if s.get("layout") == "single" and len(cards) == 1:
                p = designer.by_id.get(cards[0].get("panel")) or {}
                for g in p.get("gutter_segments") or []:
                    if isinstance(g, (list, tuple)) and len(g) == 2 \
                            and float(g[1]) > float(g[0]):
                        segs.append((float(g[0]), float(g[1])))
            dur = float(s.get("end") or 0) - float(s.get("start") or 0)
            if len(segs) < 2 or dur < len(segs) * PF_MIN_SHOT:
                expanded.append(s)
                continue
            heights = [b - a for a, b in segs]
            total_h = sum(heights) or 1.0
            spans = [dur * h / total_h for h in heights]
            for _ in range(len(spans)):
                small = [i for i, v in enumerate(spans) if v < PF_MIN_SHOT - 1e-9]
                if not small:
                    break
                big = max(range(len(spans)), key=lambda i: spans[i])
                need = PF_MIN_SHOT - spans[small[0]]
                if big == small[0] or spans[big] - need < PF_MIN_SHOT:
                    break
                spans[big] -= need
                spans[small[0]] = PF_MIN_SHOT
            t = float(s.get("start") or 0)
            for i, ((f0, f1), piece) in enumerate(zip(segs, spans)):
                ns = dict(s)
                ns["start"] = round(t, 3)
                ns["end"] = round(t + piece, 3) if i < len(segs) - 1 else s["end"]
                card = dict(cards[0])
                card["slice"] = [round(f0, 4), round(f1, 4)]
                card["at"] = 0.0
                ns["cards"] = [card]
                lo, hi = float(ns["start"]), float(ns["end"])
                ns["effects"] = [e for e in (s.get("effects") or [])
                                 if lo - 1e-9 <= float(e.get("t") or 0) < hi + 1e-9]
                ns["callouts"] = [c for c in (s.get("callouts") or [])
                                  if lo - 1e-9 <= float(c.get("t") or 0) < hi + 1e-9]
                expanded.append(ns)
                t = float(ns["end"])
            pid = cards[0].get("panel")
            if pid and pid not in report["segments"]:
                report["segments"].append(pid)
        shots[:] = expanded
        for idx, s in enumerate(shots):
            s["id"] = f"S{idx + 1:03d}"
    # Group the split pieces: same sentence text AND same panel list,
    # back to back. Shots without a sentence (fillers) never group.
    # A gutter segment is part of the key: two sub-shots of one panel
    # showing DIFFERENT segments are different pictures, never one span.
    grouped: List[Tuple[Any, List[Dict[str, Any]]]] = []
    for s in shots:
        text = str(s.get("sentence") or "")
        panels = tuple((c.get("panel"), tuple(c.get("slice") or ()))
                       for c in (s.get("cards") or []))
        key = (text, panels) if text and panels and all(p for p, _sl in panels) else None
        if key is not None and grouped and grouped[-1][0] == key:
            grouped[-1][1].append(s)
        else:
            grouped.append((key, [s]))
    drift_groups = 0  # drift-span ordinal: direction alternates, as measured
    prev_dirs: Dict[Any, str] = {}  # panel -> drift direction of the previous span
    for _key, gshots in grouped:
        g_start = float(gshots[0].get("start") or 0)
        g_end = float(gshots[-1].get("end") or 0)
        span = max(0.0, g_end - g_start)
        group_dirs: Dict[Any, str] = {}
        group_moving = False
        for s in gshots:
            dur = max(0.0, float(s.get("end") or 0) - float(s.get("start") or 0))
            elapsed = max(0.0, float(s.get("start") or 0) - g_start)
            impact, rule = panelfit_is_impact(s)
            moving = False
            for c in s.get("cards") or []:
                pid = c.get("panel")
                p = designer.by_id.get(pid) or {}
                pw, ph = float(p.get("width") or 0), float(p.get("height") or 0)
                _sl = c.get("slice")
                if isinstance(_sl, (list, tuple)) and len(_sl) == 2 and ph > 0:
                    # A gutter segment (analysis fix 1): the card shows
                    # only this band of the panel, so it fits and moves
                    # as a panel of the segment's own height.
                    ph = ph * (float(_sl[1]) - float(_sl[0]))
                if pw > 0 and ph > 0:
                    kind = panelfit_fit(pw, ph, fmt, dur=span, pcfg=pcfg)[0]
                    direction = None
                    if kind == "drift":
                        if pid in group_dirs:
                            direction = group_dirs[pid]
                        elif pid in prev_dirs:
                            # Consecutive sentences on one panel keep the
                            # direction that panel already drifts in.
                            direction = prev_dirs[pid]
                        else:
                            direction = "down" if drift_groups % 2 == 0 else "up"
                            drift_groups += 1
                        group_dirs[pid] = direction
                    c["panelfit"] = panelfit_spec(pw, ph, str(c.get("motion") or ""), fmt, pcfg,
                                                  dur=dur, impact=impact, span_dur=span,
                                                  elapsed=elapsed, direction=direction)
                    aspect = pw / ph
                    uncapped = (max(fmt.width / pw, fmt.height / ph) if aspect >= fill_aspect
                                else min(fmt.width / pw, fmt.height / ph))
                    if uncapped > cap and pid not in report["over_cap"]:
                        report["over_cap"].append(pid)
                else:
                    rate = float(pcfg.get("zoom_rate", PF_ZOOM_RATE))
                    zcap = 1.0 + float(pcfg.get("zoom_max_total", PF_ZOOM_MAX_TOTAL))
                    c["panelfit"] = {"kind": "zoom", "dir": "in", "bg": True,
                                     "base_w": float(fmt.width), "base_h": float(fmt.height),
                                     "zoom_rate": rate,
                                     "zoom_start": round(min(1.0 + rate * elapsed, zcap), 4),
                                     "zoom_end": round(min(1.0 + rate * span, zcap), 4),
                                     "drift_frac": 0.0, "impact": bool(impact)}
                spec = c["panelfit"]
                if spec["kind"] == "drift":
                    if spec.get("drift_frac"):
                        moving = True
                elif spec.get("zoom_rate", 0.0) > 0 \
                        and spec.get("zoom_start", 1.0) < spec.get("zoom_end", 1.0):
                    moving = True
            if moving:
                group_moving = True
            else:
                report["hold"].append(s.get("id"))
            if rule:
                report["impact"].append({"id": s.get("id"), "rule": rule,
                                         "zoom_rate": float(pcfg.get("zoom_rate_impact",
                                                                     PF_ZOOM_RATE_IMPACT))})
        if not group_moving:
            report["static"].extend(s.get("id") for s in gshots)
        prev_dirs = group_dirs
    # A hold entry only means something when the span itself never
    # moved; a fully static span is already reported as static.
    report["hold"] = [i for i in report["hold"] if i not in set(report["static"])]
    return report


def title_card_overlays(windows: List[Dict[str, Any]] | None,
                        pcfg: Dict[str, Any] | None) -> List[Dict[str, Any]]:
    """Timeline-cue sentences -> title-card OVERLAYS (analysis fix 3).

    The user's rule (2026-10-23): a title card is a visual overlay
    triggered at its cue — never a hole cut into the timeline. The old
    behaviour REPLACED the shots under the cue: it consumed plan time,
    split the straddling shot, and froze the picture's motion for the
    card's whole span. Now the shots — and with them every shot's
    timing and motion phase — are left exactly as planned, and each
    cue yields one overlay {"at", "duration", "label", "number",
    "line", "sentence"}: the renderer draws the split-flap card OVER
    whatever shot is running at `at`, and the master audio (muxed
    whole at render, never per-shot) plays straight through
    underneath — it is never paused, split or muted for a card.

    `at` is the cue sentence's aligned start: every cue shape the
    detector accepts OPENS its sentence ("Age 4...", "Three years
    later...", "Week 6 of..."), so the sentence start is the cue
    word's whisper timestamp (the alignment carries sentence-level
    timings only). An Age fragment ("Age 4.") merges with a directly
    following unit cue ("Week 6 of possession.") into one overlay
    whose span covers both sentences, exactly as the reference's
    narration pairs them; the on-screen duration is that span clamped
    to the measured [1.4, 2.1]s window.
    """
    from . import titlecards as _tc
    pcfg = pcfg or {}
    if not bool(pcfg.get("title_cards", PF_TITLE_CARDS)) or not windows:
        return []
    wins = sorted((w for w in windows
                   if isinstance(w, dict) and w.get("text") and w.get("end") is not None),
                  key=lambda w: float(w.get("start") or 0.0))
    cues: List[Tuple[float, float, Dict[str, Any], str]] = []
    i = 0
    while i < len(wins):
        w = wins[i]
        cue = _tc.detect_title_cue(w.get("text"))
        if cue is not None and cue["line"] == "" and i + 1 < len(wins):
            nxt = _tc.detect_title_cue(wins[i + 1].get("text"))
            if nxt is not None and nxt["line"] and nxt["label"] != "Age":
                cue = {**cue, "line": nxt["line"]}
                cues.append((float(w.get("start") or 0.0), float(wins[i + 1]["end"]),
                             cue, f"{w.get('text')} {wins[i + 1].get('text')}"))
                i += 2
                continue
        if cue is not None:
            cues.append((float(w.get("start") or 0.0), float(w["end"]), cue,
                         str(w.get("text"))))
        i += 1
    return [{"at": round(cs, 3), "duration": _tc.card_duration(ce - cs),
             "label": cue["label"], "number": int(cue["number"]),
             "line": cue["line"], "sentence": text[:300]}
            for cs, ce, cue, text in cues]


# ------------------------------------------------------------------ fullframe mode

# Full-frame mode ("video.mode: fullframe") drops the card entirely: every
# shot is a full-bleed 1920x1080 camera viewport travelling over the panel.
# The classic focal crop (or a fixes.json crop) says WHERE the camera looks;
# this section resolves it into a camera path: a `window` (the panel region
# the shot covers) plus `from`/`to` viewports -- both exactly 16:9, both
# inside the window -- that the renderer interpolates between:
#   * window taller than the viewport -> a vertical pan (the webtoon scroll);
#   * window wider than the viewport  -> a horizontal pan;
#   * window ~16:9                     -> a zoom (push_in / pull_out), or a
#     still frame for a hand-set `hold`.
# Timing, panels and `at` offsets are NEVER touched (the sync audit reads
# them), so a full-frame plan audits exactly like the classic plan it was
# derived from. Captions overlay the art (no bar), like wide mode.
FF_ASPECT = 16.0 / 9.0
FF_MIN_TRAVEL = 1.12    # a pan window must exceed its viewport by >=12% or it is a zoom instead
FF_MIN_ZOOM = 1.03      # below this depth a zoom is a still frame; reported, never disguised
FF_CAPTION_ZONE = WIDE_CAPTION_ZONE   # bottom share of the viewport the overlaid captions occupy
# Every full-frame style parameter is a named constant (the HARDCODE-IT
# rule: style lives in code/config, never in an agent's head). The three
# user-facing knobs live in config (video.fullframe.*, DEFAULTS in
# config.py); these constants are their in-code defaults and the single
# fallback every .get() uses -- a test pins the two to each other.
FF_MAX_UPSCALE = 2.4    # default of video.fullframe.max_upscale
FF_ZOOM_DEPTH = 1.18    # default of video.fullframe.zoom_depth
FF_PAN_TRAVEL = 1.0     # default of video.fullframe.pan_travel
FF_PX_TOL = 1.0         # panel-pixel tolerance for containment / floor comparisons
FF_CONTAIN_TOL = 0.002  # fraction tolerance for the viewport-inside-window checks
FF_ASPECT_TOL = 0.02    # log-space tolerance of the QA gate's exact-16:9 check


def _ff_place(f0: float, flen: float, win: float, extent: float,
              bands: List[Tuple[float, float]]) -> float:
    """Start coordinate for a window of length `win` on one axis: contains
    the focal span [f0, f0+flen] when it fits (wide_crop's containment
    clamp), centres on it when it cannot, then safe_span nudges the edges
    off content bands outside the focal."""
    if flen <= win:
        lo = max(0.0, f0 + flen - win)
        hi = min(f0, max(0.0, extent - win))
        anchor = max(lo, min(hi, f0))
    else:
        lo, hi = 0.0, max(0.0, extent - win)
        anchor = max(lo, min(hi, f0 + flen / 2.0 - win / 2.0))
    return safe_span(extent, win, lo, hi, bands, anchor)


def fullframe_viewport(pw: float, ph: float, box: List[float], motion: str, *,
                       min_vp_w: float, min_vp_h: float,
                       zoom_depth: float = FF_ZOOM_DEPTH, pan_travel: float = FF_PAN_TRAVEL,
                       img: Image.Image | None = None
                       ) -> Tuple[Dict[str, List[float]], Dict[str, Any]]:
    """Resolve one card's camera path over a panel of (pw, ph) pixels.

    `box` is the focal crop in panel fractions (the classic planner's crop,
    or a fixes.json reviewer crop -- either way it defines where the camera
    looks). `min_vp_w` / `min_vp_h` are the zoom guardrail in panel pixels:
    no viewport shown is smaller than that when the panel can supply it
    (config video.fullframe.max_upscale: frame 1920x1080 / FF_MAX_UPSCALE
    = 800x450 at the default).

    Returns ({"window", "from", "to"} in panel fractions, info) where info
    carries the resolved "motion", the path "kind" ("zoom" | "pan_y" |
    "pan_x"), and honest flags: "static" (from == to: no camera motion was
    possible), "over_cap" (a viewport below the guardrail floor -- the
    panel itself is too small to supply more), "focal_overflow" (the focal
    cannot fit inside any single 16:9 viewport on this panel, so it is
    traversed or centred, never wholly framed at once) and "caption_risk"
    (panel text sits in the bottom caption zone of the from/to viewport).
    """
    try:
        bx0, by0, bx1, by1 = (float(v) for v in box[:4])
    except (TypeError, ValueError, IndexError):
        bx0, by0, bx1, by1 = 0.0, 0.0, 1.0, 1.0
    bx0 = max(0.0, min(1.0, bx0)); by0 = max(0.0, min(1.0, by0))
    bx1 = max(bx0, min(1.0, bx1)); by1 = max(by0, min(1.0, by1))
    fx0, fy0 = bx0 * pw, by0 * ph
    fw, fh = max(1.0, (bx1 - bx0) * pw), max(1.0, (by1 - by0) * ph)
    bands_y: List[Tuple[float, float]] = []
    bands_x: List[Tuple[float, float]] = []
    if img is not None:
        bands_y = content_bands(img, axis="y", exclude=[(by0, by1)])
        bands_x = content_bands(img, axis="x", exclude=[(bx0, bx1)])
    info: Dict[str, Any] = {"motion": motion, "kind": "zoom", "static": False,
                            "over_cap": False, "focal_overflow": False, "caption_risk": False}
    depth_target = max(1.0, float(zoom_depth))
    travel = max(0.0, float(pan_travel))

    def zoom_path(depth_wanted: float) -> Tuple[Tuple[float, float, float, float],
                                                Tuple[float, float, float, float]]:
        """(outer, inner) 16:9 rects in panel pixels; outer contains the
        focal when any 16:9 viewport on this panel can, inner is outer
        shrunk by the (guardrail-capped) zoom depth around the focal."""
        ow = max(fw, fh * FF_ASPECT)
        oh = ow / FF_ASPECT
        if oh < min_vp_h * depth_wanted:      # room to zoom AND stay above the floor
            oh = min_vp_h * depth_wanted
            ow = oh * FF_ASPECT
        if ow > pw:
            ow, oh = pw, pw / FF_ASPECT
        if oh > ph:
            oh, ow = ph, ph * FF_ASPECT
        if fw > ow + FF_PX_TOL or fh > oh + FF_PX_TOL:
            info["focal_overflow"] = True
        x0 = _ff_place(fx0, fw, ow, pw, bands_x)
        y0 = _ff_place(fy0, fh, oh, ph, bands_y)
        outer = (x0, y0, ow, oh)
        depth = min(depth_wanted, ow / max(1.0, min_vp_w), oh / max(1.0, min_vp_h))
        depth = max(1.0, depth)
        iw, ih = ow / depth, oh / depth
        ix = max(x0, min(x0 + ow - iw, fx0 + fw / 2.0 - iw / 2.0))
        iy = max(y0, min(y0 + oh - ih, fy0 + fh / 2.0 - ih / 2.0))
        ix = safe_span(pw, iw, x0, x0 + ow - iw, bands_x, ix)
        iy = safe_span(ph, ih, y0, y0 + oh - ih, bands_y, iy)
        return outer, (ix, iy, iw, ih)

    def pan_y_path() -> Tuple[Tuple[float, float, float, float],
                              Tuple[float, float, float, float],
                              Tuple[float, float, float, float]] | None:
        """(window, first, last) for a vertical pan, or None when the panel
        cannot give the viewport at least FF_MIN_TRAVEL of travel."""
        vp_w = min(pw, max(fw, min_vp_w))
        vp_h = vp_w / FF_ASPECT
        win_h = min(ph, max(fh, vp_h * (1.0 + travel)))
        if win_h < vp_h * FF_MIN_TRAVEL:
            return None
        wx = _ff_place(fx0, fw, vp_w, pw, bands_x)
        wy = _ff_place(fy0, fh, win_h, ph, bands_y)
        window = (wx, wy, vp_w, win_h)
        top = (wx, wy, vp_w, vp_h)
        bottom = (wx, wy + win_h - vp_h, vp_w, vp_h)
        return window, top, bottom

    def pan_x_path() -> Tuple[Tuple[float, float, float, float],
                              Tuple[float, float, float, float],
                              Tuple[float, float, float, float]] | None:
        """(window, first, last) for a horizontal pan (pan_left semantics:
        the camera starts on the right and travels left), or None."""
        vp_h = min(ph, max(fh, min_vp_h))
        vp_w = vp_h * FF_ASPECT
        win_w = min(pw, max(fw, vp_w * (1.0 + travel)))
        if win_w < vp_w * FF_MIN_TRAVEL:
            return None
        wy = _ff_place(fy0, fh, vp_h, ph, bands_y)
        wx = _ff_place(fx0, fw, win_w, pw, bands_x)
        window = (wx, wy, win_w, vp_h)
        left = (wx, wy, vp_w, vp_h)
        right = (wx + win_w - vp_w, wy, vp_w, vp_h)
        return window, right, left   # pan_left: from = right, to = left

    window = frm = to = None
    want = motion if motion in MOTIONS else "push_in"
    if want == "hold":
        outer, _inner = zoom_path(1.0)
        window = frm = to = outer
        info["motion"] = "hold"
    elif want in ("pan_down", "pan_up"):
        got = pan_y_path()
        if got is not None:
            window, top, bottom = got
            frm, to = (top, bottom) if want == "pan_down" else (bottom, top)
            info["motion"], info["kind"] = want, "pan_y"
        else:   # no travel on this panel: a zoom moves instead (wide mode's swap)
            info["motion"] = "push_in" if want == "pan_down" else "pull_out"
            outer, inner = zoom_path(depth_target)
            window = outer
            frm, to = (outer, inner) if info["motion"] == "push_in" else (inner, outer)
    elif want in ("pan_left", "pan_right"):
        got = pan_x_path()
        if got is not None:
            window, first, last = got
            frm, to = (first, last) if want == "pan_left" else (last, first)
            info["motion"], info["kind"] = want, "pan_x"
        else:
            info["motion"] = "pull_out" if want == "pan_left" else "push_in"
            outer, inner = zoom_path(depth_target)
            window = outer
            frm, to = (outer, inner) if info["motion"] == "push_in" else (inner, outer)
    else:   # push_in / pull_out
        # A focal no 16:9 viewport on this panel can contain (a tall figure,
        # a full-width strip section) is traversed by a scroll, not sliced
        # by a static zoom window -- when the panel offers the travel.
        fits = max(fw, fh * FF_ASPECT) <= pw + FF_PX_TOL and max(fh, fw / FF_ASPECT) <= ph + FF_PX_TOL
        got = None if fits else pan_y_path()
        if got is not None:
            window, top, bottom = got
            frm, to = top, bottom
            info["motion"], info["kind"] = "pan_down", "pan_y"
        else:
            outer, inner = zoom_path(depth_target)
            window = outer
            frm, to = (outer, inner) if want == "push_in" else (inner, outer)
            info["motion"] = want

    assert window is not None and frm is not None and to is not None
    info["static"] = frm == to
    smallest_w = min(frm[2], to[2])
    smallest_h = min(frm[3], to[3])
    info["over_cap"] = smallest_w < min_vp_w - FF_PX_TOL or smallest_h < min_vp_h - FF_PX_TOL
    if bands_y:
        for vy0, vh in ((frm[1], frm[3]), (to[1], to[3])):
            zone_top = (vy0 + vh * (1.0 - FF_CAPTION_ZONE)) / ph
            zone_bot = (vy0 + vh) / ph
            if any(b0 < zone_bot and b1 > zone_top for b0, b1 in bands_y):
                info["caption_risk"] = True
                break

    def fin(rect: Tuple[float, float, float, float]) -> List[float]:
        x, y, w, h = rect
        return [round(x / pw, 4), round(y / ph, 4), round((x + w) / pw, 4), round((y + h) / ph, 4)]

    win_f = fin(window)
    out: Dict[str, List[float]] = {"window": win_f, "from": fin(frm), "to": fin(to)}
    for key in ("from", "to"):   # rounding may push an edge a hair outside the window
        r = out[key]
        r[0] = max(win_f[0], min(r[0], win_f[2]))
        r[1] = max(win_f[1], min(r[1], win_f[3]))
        r[2] = max(r[0], min(r[2], win_f[2]))
        r[3] = max(r[1], min(r[3], win_f[3]))
    return out, info


def _fullframe_style_card(c: Dict[str, Any], designer: "_Designer", pw: float, ph: float,
                          min_vp_w: float, min_vp_h: float,
                          fcfg: Dict[str, Any]) -> Dict[str, Any]:
    """Rewrite one card in place for full-frame mode: its classic crop (or
    fixes.json focal) becomes the seed the camera path is derived from; the
    card gains a "viewport" {window, from, to} and keeps `crop` = the window
    so other tooling still sees the region the shot covers. Returns the
    fullframe_viewport info dict for the pass's risk lists."""
    pid = c.get("panel")
    img = designer.img(pid) if pid in designer.by_id else None
    box = list(c.get("crop") or [0, 0, 1, 1])
    if img is not None and pid not in (designer.fixes.get("crop") or {}):
        # Same figure hardening as wide mode: the classic focal can exclude
        # part of the figure (head found, body cut). Grow the seed to cover
        # the detected head AND the connected body before deriving the path
        # (a reviewer-set crop is never second-guessed).
        head = head_of(_panel_maps(img, designer.map_options))
        if head.get("figure") and head.get("top") is not None and head.get("bottom") is not None:
            top, bottom = extend_figure_range(img, float(head["top"]), float(head["bottom"]),
                                              max_span=1.0)
            box[1] = min(box[1], top)
            box[3] = max(box[3], bottom)
    vp, info = fullframe_viewport(pw, ph, box, str(c.get("motion") or "push_in"),
                                  min_vp_w=min_vp_w, min_vp_h=min_vp_h,
                                  zoom_depth=float(fcfg.get("zoom_depth", FF_ZOOM_DEPTH)),
                                  pan_travel=float(fcfg.get("pan_travel", FF_PAN_TRAVEL)),
                                  img=img)
    c["viewport"] = vp
    c["crop"] = list(vp["window"])
    c["motion"] = info["motion"]
    c["pad_y"] = 0.0
    c["fullframe"] = True
    info["reviewer_focal_ignored"] = bool(
        pid in (designer.fixes.get("crop") or {}) and info.get("focal_overflow"))
    return info


def _fullframe_pass(shots: List[Dict[str, Any]], designer: "_Designer", fmt: Format,
                    fcfg: Dict[str, Any]) -> Dict[str, List[str]]:
    """Rewrite a classic plan for full-frame mode: every shot becomes a
    full-bleed camera move (see fullframe_viewport). Pairs keep both cards
    (the renderer shows them one after another, full-frame, cutting at the
    second card's `at`); every other layout collapses to its first card.
    caption_bar is off (captions overlay the art). Timing, panels and `at`
    are untouched, so the sync audit sees the identical plan.

    Returns panel-id lists for the honest-report fields on the plan:
    caption_risk (text in a viewport's caption zone), figure_risk (focal
    cannot fit one 16:9 viewport), reviewer_focal_ignored (a fixes.json
    focal that overflowed), over_cap (viewport below the upscale floor --
    panel too small), and static (shot ids where no camera motion was
    possible on the source pixels available).
    """
    empty: Dict[str, List[str]] = {"caption_risk": [], "figure_risk": [],
                                   "reviewer_focal_ignored": [], "over_cap": [], "static": []}
    if fmt.width <= fmt.height:
        return empty   # portrait formats untouched (Shorts keep the card look)
    fcfg = fcfg or {}
    max_upscale = max(1.0, float(fcfg.get("max_upscale", FF_MAX_UPSCALE)))
    min_vp_w = fmt.width / max_upscale     # guardrail floors, in panel pixels
    min_vp_h = fmt.height / max_upscale    # (same convention as wide's min_win_h)
    risks: Dict[str, List[str]] = {k: [] for k in empty}
    for s in shots:
        s["fullframe"] = True
        s["caption_bar"] = False
        cards = s.get("cards") or []
        pair = s.get("layout") == "pair" and len(cards) >= 2
        if not pair:
            s["layout"] = "single"
            cards = cards[:1]
        shot_static = bool(cards)
        for c in cards:
            pid = c.get("panel")
            p = designer.by_id.get(pid) or {}
            pw, ph = float(p.get("width") or 0), float(p.get("height") or 0)
            if pw > 0 and ph > 0:
                info = _fullframe_style_card(c, designer, pw, ph, min_vp_w, min_vp_h, fcfg)
                for key in ("caption_risk", "figure_risk", "reviewer_focal_ignored", "over_cap"):
                    if info.get(key) and pid not in risks[key]:
                        risks[key].append(pid)
                if not info.get("static"):
                    shot_static = False
            else:
                c["pad_y"] = 0.0
                c["fullframe"] = True
                c["viewport"] = {"window": list(c.get("crop") or [0, 0, 1, 1]),
                                 "from": list(c.get("crop") or [0, 0, 1, 1]),
                                 "to": list(c.get("crop") or [0, 0, 1, 1])}
                shot_static = False
        if shot_static and str(s.get("id")) not in risks["static"]:
            risks["static"].append(str(s.get("id")))
        s["cards"] = cards
    return risks


def build_plan(*, chapter: str, panels_data: Dict[str, Any], duration: float, audio: str | None,
               vcfg: Dict[str, Any], image_for: Callable[[Dict[str, Any]], Image.Image],
               sentences: List[Dict[str, Any]] | None = None,
               sync_report: Dict[str, Any] | None = None,
               alignment_units: List[Dict[str, Any]] | None = None,
               fixes: Dict[str, Any] | None = None,
               alignment_meta: Dict[str, Any] | None = None,
               settings: Dict[str, float] | None = None,
               root: Path | None = None,
               caption_sentences: List[Dict[str, Any]] | None = None) -> Dict[str, Any]:
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
    _pf_mode = str(vcfg.get("mode") or "classic") == "panelfit"
    _pcfg = vcfg.get("panelfit") or {}
    if _pf_mode:
        # Panel-fit (measured reference) rhythm: the mode's own shot
        # floor replaces the classic 3.0s hold floor everywhere the
        # floor is consulted for this plan (hold groups, beat snaps).
        st = {**st, "min_hold": float(_pcfg.get("min_shot", PF_MIN_SHOT))}
    timing = None
    if alignment_units is not None:
        shots_units, timing = hold_groups(alignment_units, st["min_hold"], lead=st["lead"],
                                          max_late=st["max_late"])
        units = _pair_up(shots_units, chapter, float(vcfg.get("pair_ratio", 0.3)), by_id, timed=True)
        # Carries the real whisper outcome through ("synced"/"estimated"/"even"),
        # not a generic "aligned" label - video render's refusal of an
        # "estimated" plan must keep working exactly as it did before this.
        mode = (alignment_meta or {}).get("sync_mode") or "aligned"
    elif sentences:
        units = _pair_up(synced_units(panels, sentences, duration, vcfg), chapter,
                         float(vcfg.get("pair_ratio", 0.3)), by_id, timed=True)
        mode = "synced"
    else:
        shots_units, timing = hold_groups(even_units(panels, duration, vcfg), st["min_hold"],
                                          lead=st["lead"], max_late=st["max_late"])
        units = _pair_up(shots_units, chapter, float(vcfg.get("pair_ratio", 0.3)), by_id)
        mode = "even"
    # A system-window banner in a 2-panel unit gets its own wide shot:
    # split such units before the designer ever sees a pair with one.
    units = split_system_window_units(units, by_id)
    designer = _Designer(chapter, panels, fmt, vcfg, image_for, fixes,
                         root=root, fingerprint=panels_data.get("fingerprint"))
    shots = [designer.shot(i, u) for i, u in enumerate(units)]
    # P3 editor layer: split frozen >8s shots with a punch-in crop change,
    # then attach chapter cards + SFX. Callouts already ride on each shot.
    _pacing_before = pacing_qa(shots)
    if _pf_mode:
        # Measured rhythm: sentences longer than the mode's split
        # threshold become two shots via the same split machinery
        # (the second piece is the punch-in); pieces stay >= the
        # mode's split_min_piece by construction (ceil halves).
        shots, _nsplit = split_long_shots(
            shots, max_hold=float(_pcfg.get("split_above", PF_SPLIT_ABOVE)))
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
    _captions_on = bool(vcfg.get("captions", True))
    if _pf_mode and not bool(_pcfg.get("captions", PF_CAPTIONS)):
        # Measured reference style: NO burned-in captions in this mode.
        # The mode's pin wins over the generic video.captions switch.
        _captions_on = False
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
    _mode = str(vcfg.get("mode") or "classic")
    _wide_risk = {"caption_risk": [], "figure_risk": [], "reviewer_focal_ignored": []}
    _pf_report = {"static": [], "hold": [], "over_cap": [], "impact": [], "segments": []}
    _ff_risk = {"caption_risk": [], "figure_risk": [], "reviewer_focal_ignored": [],
                "over_cap": [], "static": []}
    if _mode == "wide":
        _wide_risk = _wide_pass(shots, designer, fmt, vcfg.get("wide") or {})
    elif _mode == "adaptive":
        _wide_risk = _adaptive_pass(shots, designer, fmt, vcfg.get("adaptive") or {}, vcfg.get("wide") or {})
    elif _mode == "panelfit":
        _pf_report = _panelfit_pass(shots, designer, fmt, vcfg.get("panelfit") or {})
    elif _mode == "fullframe":
        _ff_risk = _fullframe_pass(shots, designer, fmt, vcfg.get("fullframe") or {})
    _pf_title_cards: List[Dict[str, Any]] = []
    if _mode == "panelfit":
        # Title Card Generator: timeline-cue sentences become card
        # OVERLAYS (analysis fix 3) — the shots are not touched.
        # Windows come from the alignment segments (via
        # caption_sentences) or, on the cues path, from `sentences`
        # (ends derived from the next sentence's start).
        _wins: List[Dict[str, Any]] = []
        for _w in (caption_sentences if caption_sentences is not None else sentences) or []:
            if isinstance(_w, dict) and _w.get("text") and _w.get("start") is not None:
                _wins.append({"text": _w.get("text"), "start": float(_w["start"]),
                              "end": (float(_w["end"]) if _w.get("end") is not None else None)})
        _wins.sort(key=lambda w: w["start"])
        for _i, _w in enumerate(_wins):
            if _w["end"] is None:
                _w["end"] = float(_wins[_i + 1]["start"]) if _i + 1 < len(_wins) else float(duration)
        _pf_title_cards = title_card_overlays(_wins, vcfg.get("panelfit") or {})
    return {
        "version": PLAN_VERSION,
        "mode": _mode,
        "wide_caption_risk": _wide_risk["caption_risk"],
        "wide_figure_risk": _wide_risk["figure_risk"],
        "wide_reviewer_focal_ignored": _wide_risk["reviewer_focal_ignored"],
        "panelfit_static": _pf_report["static"],
        "panelfit_hold": _pf_report["hold"],
        "panelfit_over_cap": _pf_report["over_cap"],
        "panelfit_impact": _pf_report["impact"],
        "panelfit_segments": _pf_report["segments"],
        "panelfit_title_cards": _pf_title_cards,
        "title_card_overlays": _pf_title_cards,
        "fullframe_caption_risk": _ff_risk["caption_risk"],
        "fullframe_figure_risk": _ff_risk["figure_risk"],
        "fullframe_reviewer_focal_ignored": _ff_risk["reviewer_focal_ignored"],
        "fullframe_over_cap": _ff_risk["over_cap"],
        "fullframe_static": _ff_risk["static"],
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
                   # Rule 1: max shots = floor(audio_seconds / 3). More shots
                   # than this means the average shot is under the 3s floor.
                   # Panel-fit mode runs the measured reference rhythm
                   # instead: cap = floor(audio_seconds / 1.0), floor = the
                   # mode's own min_shot.
                   "max_shots": max(0, int(float(duration) // (1.0 if _pf_mode else MIN_SHOT))),
                   "min_shot": (float(_pcfg.get("min_shot", PF_MIN_SHOT))
                                if _pf_mode else MIN_SHOT)},
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
        if not isinstance(cards, list) or (not cards and not s.get("title_card")):
            errs.append(f"{where}: needs at least one card")
            cards = []
        _tc_spec = s.get("title_card")
        if _tc_spec is not None and not (
                isinstance(_tc_spec, dict)
                and isinstance(_tc_spec.get("label"), str)
                and isinstance(_tc_spec.get("line"), str)
                and isinstance(_tc_spec.get("number"), int)):
            errs.append(f"{where}: title_card must be {{label: str, number: int, line: str}} "
                        "(video/titlecards.py writes it; rebuild the plan with --force)")
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
            if c.get("viewport") is not None:
                # Full-frame camera path: structural check only (the 16:9
                # aspect itself needs the panel's pixel size -- QA in
                # build.py checks that; here: boxes valid, from/to inside
                # the window).
                vp = c.get("viewport") or {}
                try:
                    boxes = {k: [float(v) for v in vp[k]] for k in ("window", "from", "to")}
                    w = boxes["window"]
                    ok = all(0 <= b[0] < b[2] <= 1 and 0 <= b[1] < b[3] <= 1 for b in boxes.values())
                    for k in ("from", "to"):
                        r = boxes[k]
                        ok = ok and r[0] >= w[0] - FF_CONTAIN_TOL and r[1] >= w[1] - FF_CONTAIN_TOL \
                            and r[2] <= w[2] + FF_CONTAIN_TOL and r[3] <= w[3] + FF_CONTAIN_TOL
                    if not ok:
                        raise ValueError
                except (KeyError, TypeError, ValueError):
                    errs.append(f"{cw}: viewport must be {{\"window\", \"from\", \"to\"}} panel-fraction boxes "
                                f"with from/to inside the window (video.mode: fullframe)")
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
            _pf = c.get("panelfit")
            if _pf is not None:
                _pf_ok = (isinstance(_pf, dict)
                          and _pf.get("kind") in ("zoom", "drift", "fill", "scroll")
                          and _pf.get("dir") in (("up", "down") if _pf.get("kind") in ("scroll", "drift")
                                                 else ("in", "out")))
                if _pf_ok:
                    try:
                        _pf_ok = (float(_pf["base_w"]) > 0 and float(_pf["base_h"]) > 0
                                  and float(_pf["zoom_end"]) > 0)
                        if _pf.get("kind") in ("zoom", "fill"):
                            _pf_ok = _pf_ok and float(_pf["zoom_rate"]) > 0 \
                                and float(_pf["zoom_end"]) <= 1.0 + PF_ZOOM_MAX_TOTAL + 1e-6
                            if "zoom_start" in _pf:  # a span slice (B1d): opens mid-zoom
                                _pf_ok = _pf_ok and 1.0 - 1e-6 <= float(_pf["zoom_start"]) \
                                    <= float(_pf["zoom_end"]) + 1e-6
                        if _pf.get("kind") == "drift":
                            # drift_frac is SIGNED (B1d span slices): its sign
                            # must agree with dir, and a zero-travel slice is
                            # only legal as a cap-hold carrying drift_from.
                            _frac = float(_pf["drift_frac"])
                            _pf_ok = _pf_ok and -0.25 <= _frac <= 0.25
                            _pf_ok = _pf_ok and (_frac >= -1e-9 if _pf.get("dir") == "down"
                                                 else _frac <= 1e-9)
                            if "drift_from" in _pf:
                                _pf_ok = _pf_ok and abs(float(_pf["drift_from"])) <= 0.25 \
                                    and abs(float(_pf["drift_to"])) <= 0.25 \
                                    and abs(float(_pf["drift_rate"])) <= 0.25 \
                                    and (float(_pf["drift_rate"]) >= -1e-9 if _pf.get("dir") == "down"
                                         else float(_pf["drift_rate"]) <= 1e-9)
                            else:
                                _pf_ok = _pf_ok and _frac != 0.0
                    except (KeyError, TypeError, ValueError):
                        _pf_ok = False
                if not _pf_ok:
                    errs.append(f"{cw}: panelfit spec must be {{kind: zoom|drift|fill, dir, base_w, base_h, "
                                "zoom_rate, zoom_end, drift_frac} "
                                "(video.mode: panelfit writes it; rebuild the plan with --force)")
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
    for _ov in plan.get("title_card_overlays") or []:
        # Analysis fix 3: overlays are timed card drawings over the
        # running shots — {at, duration, label, number, line}.
        if not isinstance(_ov, dict) \
                or not isinstance(_ov.get("label"), str) \
                or not isinstance(_ov.get("line"), str) \
                or not isinstance(_ov.get("number"), int):
            errs.append("title_card_overlays entries must be {at, duration, "
                        "label: str, number: int, line: str} "
                        "(video/titlecards.py + plan.title_card_overlays write them)")
            continue
        try:
            if not 0 <= float(_ov["at"]) < max(duration, 0) \
                    or not 0 < float(_ov["duration"]) <= 2.1 + 1e-9:
                errs.append(f"title card overlay at {_ov.get('at')}s must start inside "
                            f"the video and last (0, 2.1]s")
        except (KeyError, TypeError, ValueError):
            errs.append("title_card_overlays entries need numeric \"at\" and \"duration\"")
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
