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

import json
import math
import random
import zlib
from pathlib import Path
from typing import Any, Callable, Dict, List

from PIL import Image

from ..util import UserError, atomic_write_text, read_agent_json, require_chapter
from .crops import clean_crop, tone
from .frames import BACKGROUNDS, EFFECTS, ENTRANCES, EXITS, LAYOUTS, MOTIONS, Format, crop_aspect, format_from
from .media import video_dir

PLAN_VERSION = 2
MIN_SYNC_SHOT = 1.0      # shorter shots merge into the previous one (avoids flicker)
PUNCH_GAP = 4.0
EFFECT_GRACE = 0.35

ENTRANCE_WEIGHTS = [("cut", 50), ("fade_in", 10), ("fit_in_frame", 9), ("zoom_in", 8), ("blur_in", 7),
                    ("slide_up", 5), ("pull_down", 4), ("swing_right", 4), ("slide_down", 3)]
MOTION_WEIGHTS = [("push_in", 45), ("pull_out", 20), ("pan", 25), ("hold", 10)]


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
    """Spread the audio over the panels: ~2.7s each, longer for text-heavy ones."""
    if not panels:
        raise UserError("No panels to put in the video.")
    lo = float(vcfg.get("min_shot", 1.0))
    hi = max(lo + 0.5, float(vcfg.get("max_shot", 5.0)))
    target = min(hi, max(lo, float(vcfg.get("target_shot", 2.7))))
    fits = max(1, int(duration / target))
    # Recap videos show art, not speech bubbles: the narration carries the
    # dialogue. Use text-only panels only if there's too little art to fill time.
    art = [p for p in panels if not p.get("text_only")]
    if len(art) >= min(len(panels), fits) or len(art) >= 0.5 * len(panels):
        panels = art or panels
    if len(panels) > fits:
        # More panels than the narration has time for. Squeezing them all in
        # gives frantic half-second shots, so show an evenly spaced selection:
        # story order and coverage are kept, and the pacing stays calm.
        step = len(panels) / fits
        panels = [panels[min(len(panels) - 1, int(i * step + step / 2))] for i in range(fits)]
    rounds = max(1, math.ceil(duration / (len(panels) * hi)))
    seq = [p for _ in range(rounds) for p in panels]
    weights = [1.0 + min(len(p.get("text") or ""), 300) / 150 + (0.4 if p.get("tall") else 0) for p in seq]
    n = len(seq)
    if n * lo >= duration:
        durs = [duration / n] * n
    else:
        fixed: Dict[int, float] = {}
        for _ in range(n + 1):
            free = [i for i in range(n) if i not in fixed]
            remaining = duration - sum(fixed.values())
            wsum = sum(weights[i] for i in free) or 1.0
            trial = {i: remaining * weights[i] / wsum for i in free}
            clamped = {i: (lo if d < lo else hi) for i, d in trial.items() if d < lo or d > hi}
            if not clamped:
                fixed.update(trial)
                break
            fixed.update(clamped)
        durs = [fixed[i] for i in range(n)]
    units, t = [], 0.0
    for p, d in zip(seq, durs):
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


# ------------------------------------------------------------------ shot design


class _Designer:
    """Turns timing units into shots: layouts, crops, backgrounds, animation."""

    def __init__(self, chapter: str, panels: List[Dict[str, Any]], fmt: Format, vcfg: Dict[str, Any],
                 image_for: Callable[[Dict[str, Any]], Image.Image]):
        self.chapter, self.fmt, self.vcfg = chapter, fmt, vcfg
        self.by_id = {p["id"]: p for p in panels}
        self.image_for = image_for
        self._img: Dict[str, Image.Image] = {}
        self._crops: Dict[tuple, List[List[float]]] = {}
        self._tone: Dict[str, str] = {}
        self.uses: Dict[str, int] = {}
        self.prev_enter: str | None = None
        self.prev_bg = "blur"
        self.last_punch = -1e9

    def img(self, pid: str) -> Image.Image:
        if pid not in self._img:
            if len(self._img) > 12:
                self._img.clear()
            self._img[pid] = self.image_for(self.by_id[pid])
        return self._img[pid]

    def panel_aspect(self, pid: str) -> float:
        p = self.by_id[pid]
        return float(p.get("width") or 1) / max(1.0, float(p.get("height") or 1))

    def crop(self, pid: str, layout: str) -> List[float]:
        aspect = crop_aspect(layout, self.fmt, self.panel_aspect(pid))
        key = (pid, round(aspect, 3))
        if key not in self._crops:
            # Tall panels may be cut a little shorter than the card's tallest
            # shape, if that keeps a bubble or caption out of the picture.
            widest = crop_aspect(layout, self.fmt, 100.0)
            shapes = sorted({round(aspect, 3), round((aspect + widest) / 2, 3), round(widest, 3)})
            shapes = [a for a in shapes if a >= aspect] if aspect < widest else [aspect]
            self._crops[key] = list(clean_crop(self.img(pid), shapes))
        return self._crops[key]

    def tone(self, pid: str) -> str:
        if pid not in self._tone:
            self._tone[pid] = tone(self.img(pid))
        return self._tone[pid]

    def background(self, rng: random.Random, pid: str, layout: str) -> str:
        if layout == "inset":
            return "blur"
        # A real mix of blurred, black and white, leaning on what suits the
        # panel: light panels sit well on white, dark ones on black.
        t = self.tone(pid)
        black, white = {"white": (0.20, 0.45), "dark": (0.50, 0.15)}.get(t, (0.28, 0.20))
        r = rng.random()
        kind = "black" if r < black else ("white" if r < black + white else "blur")
        if kind != "blur" and kind == self.prev_bg:
            kind = "blur"  # never two identical solid backgrounds in a row
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

        if len(ids) >= 2 and dur >= 2.4:
            layout = "inset" if (rng.random() < 0.2 and dur >= 2.8) else "pair"
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
                if unit.get("second_at") is not None:   # arrives on its own words
                    at = round(min(max(0.6, float(unit["second_at"])), dur - 1.0), 3)
                else:
                    at = round(min(max(0.8, 0.45 * dur), dur - 1.2), 3)
                enter = "slide_up" if self.fmt.portrait else "slide_left"
                motion = "push_in"
            else:
                at = 0.0
                enter = self.entrance(rng, unit["cues"], layout)
                motion = "push_in" if "beat" in unit["cues"] else self.motion(rng, crop)
            cards.append({"panel": pid, "crop": crop, "enter": enter, "at": at, "motion": motion})

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

        shot = {
            "id": f"S{i + 1:03d}", "start": round(start, 3), "end": round(end, 3),
            "layout": layout, "background": self.background(rng, (bg_panel or first), layout),
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
            # Synced: each card must be on screen long enough for its words.
            long_enough = (not timed or (u["end"] - u["start"] >= 1.2 and v["end"] - v["start"] >= 1.5))
            if (long_enough and len(u["panels"]) == 1 and len(v["panels"]) == 1
                    and u["panels"][0] != v["panels"][0] and not wide(a) and not wide(b)
                    and (v["end"] - u["start"]) >= 2.8 and _rng(chapter, 10_000 + i).random() < ratio):
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
               sync_report: Dict[str, Any] | None = None) -> Dict[str, Any]:
    panels = panels_data["panels"]
    fmt = format_from(vcfg.get("format", "16:9"), int(vcfg.get("fps", 30)))
    by_id = {p["id"]: p for p in panels}
    if sentences:
        units = _pair_up(synced_units(panels, sentences, duration, vcfg), chapter,
                         float(vcfg.get("pair_ratio", 0.3)), by_id, timed=True)
        mode = "synced"
    else:
        units = _pair_up(even_units(panels, duration, vcfg), chapter, float(vcfg.get("pair_ratio", 0.3)), by_id)
        mode = "even"
    designer = _Designer(chapter, panels, fmt, vcfg, image_for)
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
        "music": vcfg.get("music"),
        "music_volume": float(vcfg.get("music_volume", 0.22)),
        "effects_enabled": dict(vcfg.get("effects") or {}),
        "panels_fingerprint": panels_data.get("fingerprint"),
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
