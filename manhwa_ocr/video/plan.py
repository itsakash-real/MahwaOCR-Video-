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
(about 2.7s on average), hard cuts, eased motion that settles and holds.
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
from .crops import _panel_maps, head_of, tone
from .cropcache import crop_key, load_crop, store_crop
from .verify import pick_card
from .frames import BACKGROUNDS, EFFECTS, ENTRANCES, EXITS, LAYOUTS, MOTIONS, Format, format_from, shapes_for
from .media import video_dir

PLAN_VERSION = 2
MIN_SYNC_SHOT = 1.0      # shorter shots merge into the previous one (avoids flicker)
MAX_SYNC_SHOT = 5.5      # max duration when merging sentences sharing the same panel
MAX_PAIR_SHOT = 6.5      # max total duration allowed for a 2-panel pair shot
PUNCH_GAP = 4.0
EFFECT_GRACE = 0.35
MIN_HOLD = 3.0           # seconds: no picture is held on screen for less than this
PAD_LIMIT = 1.5          # a card's pad_y (blurred padding above and below, in panel heights) is at most this

# Reference recap (measured): hard cuts, almost still pictures, only slight drift.
ENTRANCE_WEIGHTS = [("cut", 90), ("fade_in", 10)]
MOTION_WEIGHTS = [("hold", 55), ("push_in", 30), ("pan", 15)]


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
            # (the selected one plus any the coverage pass placed in it).
            # A hand-edited selected_panel replaces the panel the alignment chose;
            # the coverage extras placed in this sentence are kept, so no panel is
            # dropped by an edit.
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
        step = (end - start) / len(seq)
        for k, pid in enumerate(seq):
            a = start + k * step
            b = end if k == len(seq) - 1 else a + step
            add({"start": a, "end": b, "panels": [pid], "cues": [], "emphasis": [],
                 "sentence": seg.get("text") if k == 0 else None,
                 "sentence_starts": [start] if k == 0 else [a]})
    if units:
        units[0]["start"] = 0.0
    return units


def hold_groups(units: List[Dict[str, Any]], min_hold: float = MIN_HOLD, *,
                lead: float | None = None, max_late: float | None = None,
                min_hold_short: float | None = None) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """Pack timed single-panel units into shots with a bounded cut budget.

    Every cut may move only inside [sentence_start - lead, sentence_start +
    max_late]. A picture can no longer borrow seconds from a far-away
    neighbour - the old donor pass was what made the shot drift away from the
    voice. A unit shorter than min_hold shares one shot with the next unit
    (two cards, the second arriving on its own words); a shot still shorter
    than min_hold_short after that takes at most `lead` more from its start
    and `max_late` from its end. A shot that even then can't reach the floor
    is left short and counted in stats["short_shots"] - the plan QA reports
    it; the timing never silently drifts to hide it.

    Returns (shots, stats). `lead`, `max_late` and `min_hold_short` default to
    the video.sync values (sync.SYNC_DEFAULTS); build_plan passes the effective
    settings from manhwa-ocr.yaml.
    """
    lead = float(sync_mod.SYNC_DEFAULTS["lead"]) if lead is None else float(lead)
    max_late = float(sync_mod.SYNC_DEFAULTS["max_late"]) if max_late is None else float(max_late)
    min_hold = float(min_hold)
    # The floor can never be above the configured min_hold: a user who asks for
    # 1s cuts gets 1s cuts, not a 2s floor they never set.
    floor = min(min_hold, float(sync_mod.SYNC_DEFAULTS["min_hold_short"]) if min_hold_short is None
                else float(min_hold_short))
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
        self.crop_stats: Dict[str, List[str]] = {"head_risk": [], "side_cut": [], "bubble": [],
                                                 "padded": [], "white_edge": [],
                                                 "low_confidence_heads": []}
        self.map_options = map_options(vcfg)
        self._tone: Dict[str, str] = {}
        self.uses: Dict[str, int] = {}
        self.prev_enter: str | None = None
        self.prev_bg = "blur"
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

    def crop(self, pid: str, layout: str) -> List[float]:
        """The card's crop: one exact shape from the layout's list (crops.py)."""
        key = (pid, layout)
        if key not in self._crops:
            if pid in self.fixes["crop"]:                  # a reviewer's exact crop (fixes.json)
                shape, box = self.fixes["crop"][pid]
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
                               {"pick": card, "head": head, "edge_rows": list(self._maps[pid]["edge_rows"])})
            if not head["confident"] and (head["source"] == "pale" or head["keep_top"]) \
                    and pid not in self.crop_stats["low_confidence_heads"]:
                self.crop_stats["low_confidence_heads"].append(pid)
            shape, box, probs = card["shape"], card["box"], card["problems"]
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

    def background(self, rng: random.Random, pid: str, layout: str) -> str:
        if layout == "inset":
            return "blur"
        # Blurred (most shots) or black. Never white: a white screen behind
        # pale manhwa cards washes the picture out. Dark panels lean on black.
        t = self.tone(pid)
        black = {"dark": 0.40}.get(t, 0.15)
        kind = "black" if rng.random() < black else "blur"
        if kind != "blur" and kind == self.prev_bg:
            kind = "blur"  # never two black backgrounds in a row
        self.prev_bg = kind
        return kind

    def motion(self, rng: random.Random, crop: List[float]) -> str:
        m = _pick(rng, MOTION_WEIGHTS)
        if m != "pan":
            return m
        if crop[3] - crop[1] > 0.999:
            return "push_in"     # the whole panel is on the card: nothing to pan over
        return rng.choice(["pan_down", "pan_up"])

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

        if len(ids) >= 2:
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
                # A pair shorter than a second (a one-word sentence in dense
                # narration) has no room for a later entrance: the second card
                # arrives with the first inside the sentence, instead of the
                # cut drifting away from the voice.
                if unit.get("second_at") is not None:   # arrives on its own words
                    at = round(max(0.0, min(max(0.6, float(unit["second_at"])), dur - 1.0)), 3)
                else:
                    at = round(max(0.0, min(max(0.8, 0.45 * dur), dur - 1.2)), 3)
                enter = "slide_up" if self.fmt.portrait else "slide_left"
                motion = "push_in"
            else:
                at = 0.0
                enter = self.entrance(rng, unit["cues"], layout)
                motion = "push_in" if "beat" in unit["cues"] else self.motion(rng, crop)
            enter = (self.fixes["shot"].get(pid) or {}).get("enter", enter)   # fixes.json "shot" override
            card = {"panel": pid, "crop": crop, "enter": enter, "at": at, "motion": motion}
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

        background = self.background(rng, (bg_panel or first), layout)
        background = (self.fixes["shot"].get(first) or {}).get("background", background)
        shot = {
            "id": f"S{i + 1:03d}", "start": round(start, 3), "end": round(end, 3),
            "layout": layout, "background": background,
            "cards": cards, "exit": "fade_out" if last else "cut", "effects": effects,
            "sentence": (unit.get("sentence") or "")[:300] or None,
        }
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


def build_plan(*, chapter: str, panels_data: Dict[str, Any], duration: float, audio: str | None,
               vcfg: Dict[str, Any], image_for: Callable[[Dict[str, Any]], Image.Image],
               sentences: List[Dict[str, Any]] | None = None,
               sync_report: Dict[str, Any] | None = None,
               alignment_units: List[Dict[str, Any]] | None = None,
               fixes: Dict[str, Any] | None = None,
               alignment_meta: Dict[str, Any] | None = None,
               settings: Dict[str, float] | None = None,
               root: Path | None = None) -> Dict[str, Any]:
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
    """
    panels = panels_data["panels"]
    fmt = format_from(vcfg.get("format", "16:9"), int(vcfg.get("fps", 30)))
    by_id = {p["id"]: p for p in panels}
    st = settings or sync_mod.effective_settings(vcfg, log=lambda _msg: None)
    timing = None
    if alignment_units is not None:
        shots_units, timing = hold_groups(alignment_units, st["min_hold"], lead=st["lead"],
                                          max_late=st["max_late"], min_hold_short=st["min_hold_short"])
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
                                          lead=st["lead"], max_late=st["max_late"],
                                          min_hold_short=st["min_hold_short"])
        units = _pair_up(shots_units, chapter, float(vcfg.get("pair_ratio", 0.3)), by_id)
        mode = "even"
    designer = _Designer(chapter, panels, fmt, vcfg, image_for, fixes,
                         root=root, fingerprint=panels_data.get("fingerprint"))
    shots = [designer.shot(i, u, i == len(units) - 1) for i, u in enumerate(units)]
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
        "music": vcfg.get("music"),
        "music_volume": float(vcfg.get("music_volume", 0.22)),
        "effects_enabled": dict(vcfg.get("effects") or {}),
        "panels_fingerprint": panels_data.get("fingerprint"),
        "timing": timing,
        "crop_stats": designer.crop_stats,
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
