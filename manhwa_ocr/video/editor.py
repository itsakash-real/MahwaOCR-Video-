"""P3 editor layer: viral-recap editing helpers (pure functions, no I/O except
resolve_* which only stat() paths).

Research-backed rules implemented here (see the P3 plan):
- Continuous motion: every panel gets a Ken Burns move for the whole shot;
  `hold` is reserved for hand-set payoff beats, never emitted by the planner.
- Pacing: a panel holds 3-4s (5-7s only for readable text panels), never >8s
  without a crop change / second panel. Long shots are split with a punch-in.
- Callouts: burned-in KEYWORD callouts only (never every word).
- Chapter cards every ~6-11 min, stakes-based titles, whoosh + music shift.
- SFX as punctuation (whoosh / impact / ding / sting), missing files never fail.
- Music: explicit `video.music` wins, else `music/bed.mp3` in the series folder
  is used when it exists, ducked to 15-25% under narration.
- Hook / open loops: `[HOOK]` and `[LOOP: ...]` markers in the recap (editing
  cues, never spoken) ride through cues.json into the plan.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Dict, List

# ------------------------------------------------------------------ pacing

PACING_TARGET_MIN = 3.0   # panel hold target: 3-4s
PACING_TARGET_MAX = 4.0
PACING_TEXT_MAX = 7.0     # readable text panels may hold up to 7s
PACING_MAX_HOLD = 8.0     # never hold one frame longer than this without a crop change
# Split pieces are sized by ceil(dur / PACING_MAX_HOLD), so every piece lands
# in (4, 8]s: under the 8s cap, and above the 3s min_hold floor (a split
# into <=4s pieces would break min_hold for e.g. an 8.6s shot -> 2.9s pieces).

# Continuous-motion weights for the planner (plan.py MOTION_WEIGHTS mirrors this).
# `hold` is deliberately absent: the planner never emits it. A reviewer may still
# hand-set motion="hold" in fixes.json / plan.json for a deliberate 1-2s
# post-payoff silence beat; frames.py keeps the old ease-out-then-hold for it.
MOTION_WEIGHTS = [("push_in", 35), ("pull_out", 25), ("pan", 40)]
PAN_DIRECTIONS = ("pan_left", "pan_right", "pan_up", "pan_down")


def pacing_qa(shots: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Flag shots that hold one frame too long. Pure / testable.

    A shot over PACING_MAX_HOLD with a single card and no crop change is a
    frozen hold; the planner splits those (see split_long_shot). Pair shots
    already change picture when the second card arrives, so only single-card
    shots over the limit are flagged.
    """
    long_shots: List[Dict[str, Any]] = []
    for s in shots:
        try:
            dur = float(s["end"]) - float(s["start"])
        except (KeyError, TypeError, ValueError):
            continue
        if dur > PACING_MAX_HOLD and len(s.get("cards") or []) <= 1:
            long_shots.append({"shot": s.get("id"), "duration": round(dur, 3),
                               "panel": (s.get("cards") or [{}])[0].get("panel")})
    durations = [float(s["end"]) - float(s["start"]) for s in shots
                 if isinstance(s, dict) and "start" in s and "end" in s]
    return {"long_shots": long_shots,
            "max_shot": round(max(durations), 3) if durations else 0.0,
            "shots": len(shots)}


def _inset_crop(box: List[float], frac: float) -> List[float]:
    """Shrink a crop box toward its centre by `frac` of each side (punch-in).

    Clamped so the box never gets smaller than 30% wide / 30% tall: a punch-in
    must stay readable, never zoom into a postage stamp. The SAME fraction is
    used on both axes (whichever the 30% floor allows is more restrictive),
    never an independent clamp per axis: clamping width and height separately
    could stop one axis earlier than the other and skew the box's pixel
    aspect into one no allowed card shape matches, failing QA's shape check
    on exactly the punched-in piece (a split's second showing).
    """
    x0, y0, x1, y1 = (float(v) for v in box)
    w, h = x1 - x0, y1 - y0
    max_frac_w = (1.0 - 0.30 / w) / 2 if w > 1e-9 else frac
    max_frac_h = (1.0 - 0.30 / h) / 2 if h > 1e-9 else frac
    f = max(0.0, min(frac, max_frac_w, max_frac_h))
    dx, dy = w * f, h * f
    return [round(x0 + dx, 4), round(y0 + dy, 4), round(x1 - dx, 4), round(y1 - dy, 4)]


def _inset_card(card: Dict[str, Any], frac: float) -> Dict[str, Any]:
    """Punch a card's crop in by `frac` (toward centre, see _inset_crop) and
    scale its pad_y (blurred padding, in panel heights) by the SAME factor
    the crop's own height actually shrank by.

    A card with pad_y > 0 is a wide panel shown whole with blurred padding
    added above/below so the padded TOTAL (crop height + 2*pad) matches an
    allowed shape; the crop box alone is not the card's effective shape.
    Insetting only the crop (the old behaviour) left pad_y as a growing
    share of a shrinking crop, skewing that effective aspect into one no
    allowed shape matches -- QA then failed on exactly the punched-in piece
    (a split shot's second showing, or a diversity punch-in). Returns a new
    card dict; the input is never mutated.
    """
    crop = list(card.get("crop") or [0, 0, 1, 1])
    new_crop = _inset_crop(crop, frac)
    out = dict(card, crop=new_crop)
    pad = float(card.get("pad_y") or 0.0)
    if pad > 0:
        old_h = crop[3] - crop[1]
        new_h = new_crop[3] - new_crop[1]
        shrink = (new_h / old_h) if old_h > 1e-9 else 1.0
        out["pad_y"] = round(pad * shrink, 4)
    return out


def split_long_shot(shot: Dict[str, Any], max_hold: float | None = None) -> List[Dict[str, Any]]:
    """Split one >8s shot into <=8s pieces with a punch-in crop change per piece.

    Pure geometry: no image access. Every piece keeps the same panels, sentence
    and effects that fall inside it; each later piece's cards are inset by 8%
    per piece so the cut reads as a punch-in, not a frozen restart. Shots at or
    under PACING_MAX_HOLD, shots with no cards, and shots with no crops are
    returned unchanged. Piece ids are suffixed -a/-b/...; the first piece keeps
    the original id when there is only one piece (i.e. no split happened).

    `max_hold` overrides the split threshold (panel-fit mode passes its own
    3.5s sentence-split rule); None keeps the classic PACING_MAX_HOLD.
    """
    limit = PACING_MAX_HOLD if max_hold is None else float(max_hold)
    try:
        dur = float(shot["end"]) - float(shot["start"])
    except (KeyError, TypeError, ValueError):
        return [shot]
    cards = shot.get("cards") or []
    if dur <= limit or not cards:
        return [shot]
    n = max(2, int(-(-dur // limit)))  # ceil -> every piece <= limit
    piece = dur / n
    prev_motions = [c.get("motion") for c in cards]  # motion of the previous piece, per card
    out: List[Dict[str, Any]] = []
    for k in range(n):
        part = dict(shot)
        part["start"] = round(float(shot["start"]) + k * piece, 3)
        part["end"] = round(float(shot["start"]) + (k + 1) * piece, 3)
        # CapCut bugfix: only the FIRST piece arrives with the shot's
        # transition (later pieces are punch-in continuations, not new
        # arrivals), and only the LAST piece carries the shot's exit
        # (an exit copied to every piece would fade out mid-shot).
        if k > 0:
            part["transition"] = "cut"
        if k < n - 1:
            part["exit"] = "cut"
        if n > 1:
            part["id"] = f"{shot.get('id', 'S?')}-{chr(ord('a') + k)}"
        new_cards = []
        for ci, c in enumerate(cards):
            crop = c.get("crop")
            cc = _inset_card(c, 0.08 * k) if (crop and k > 0) else dict(c)
            # A later-arriving card keeps its arrival offset inside its piece.
            at = float(c.get("at") or 0)
            cc["at"] = round(max(0.0, at - k * piece), 3)
            # CapCut bugfix: a later piece must not restart a pan (each piece's
            # Card restarts q -1 -> +1, which reads as a left-right swap glitch).
            # A punch-in (crop inset above) already gives the piece its own move.
            if k > 0 and cc.get("motion") in PAN_DIRECTIONS:
                cc["motion"] = "push_in"
            # CapCut bugfix (defect 2): a later piece must not repeat the
            # previous piece's motion either (split pieces all copied the
            # same motion, reading as one frozen drift). Flip push_in <->
            # pull_out; hand-set `hold` is left alone (payoff beat).
            if k > 0 and cc.get("motion") == prev_motions[ci] and cc.get("motion") != "hold":
                cc["motion"] = "pull_out" if cc.get("motion") == "push_in" else "push_in"
            prev_motions[ci] = cc.get("motion")
            new_cards.append(cc)
        part["cards"] = new_cards
        # Effect / callout times are ABSOLUTE timeline times (like the pieces'
        # start/end), so they are kept as-is and only filtered to this piece.
        # Card "at" above is shot-relative, which is why only it is shifted.
        part["effects"] = [dict(e) for e in (shot.get("effects") or [])
                           if part["start"] <= float(e["t"]) < part["end"]]
        part["callouts"] = [dict(c) for c in (shot.get("callouts") or [])
                            if part["start"] <= float(c["t"]) < part["end"]]
        out.append(part)
    return out


def split_long_shots(shots: List[Dict[str, Any]], max_hold: float | None = None) -> tuple[List[Dict[str, Any]], int]:
    """Split every over-long shot. Returns (new shots, number of shots split)."""
    out: List[Dict[str, Any]] = []
    split = 0
    for s in shots:
        parts = split_long_shot(s, max_hold=max_hold)
        if len(parts) > 1:
            split += 1
        out.extend(parts)
    return out, split


# ------------------------------------------------------------------ callouts

_CALLOUT_MAX_LEN = 40
_CALLOUT_MAX_LINES = 2

# Rank / skill / number patterns, most specific first. A callout is the single
# most checkable promise in the sentence: a rank, a damage number, a level.
_RANK_RE = re.compile(r"\b([A-Z]{1,3}(?:\+)?-?rank|SS|SSS|S-rank|A-rank|B-rank|C-rank|D-rank|E-rank|F-rank)\b", re.IGNORECASE)
_NUMBER_RE = re.compile(r"\b\d[\d,]*(?:\.\d+)?\s*(?:million|billion|thousand|damage|HP|gold|won|dollars?|percent|%)?\b",
                        re.IGNORECASE)
_LEVEL_RE = re.compile(r"\b(?:Level|Lv\.?|Floor|Chapter)\s*\d+\b", re.IGNORECASE)
_TIME_RE = re.compile(r"\b\d+\s*(?:days?|weeks?|months?|years?)\s+later\b", re.IGNORECASE)
_PLACE_RE = re.compile(r"\b(?:Dungeon|Tower|Academy|Guild|Arena)[^,.]{0,24}", re.IGNORECASE)


def _clean_callout(text: str) -> str:
    text = re.sub(r"\s+", " ", (text or "").strip())
    if len(text) > _CALLOUT_MAX_LEN:
        text = text[:_CALLOUT_MAX_LEN - 1].rstrip() + "\u2026"
    return text.upper()


def extract_callout(sentence: str, cues: List[str]) -> Dict[str, Any] | None:
    """One keyword callout for a shot, or None.

    Only impact / reveal sentences get a callout (the peaks), and only when the
    sentence carries a checkable keyword: a rank, a level/floor, a number, a
    time skip, or a place. The callout is a keyword, never the whole sentence.
    """
    cues = set(cues or [])
    if not (cues & {"impact", "reveal"}):
        return None
    text = sentence or ""
    for rx, style in ((_RANK_RE, "keyword"), (_LEVEL_RE, "keyword"),
                      (_TIME_RE, "time"), (_PLACE_RE, "place"), (_NUMBER_RE, "number")):
        m = rx.search(text)
        if m:
            cleaned = _clean_callout(m.group(0))
            if cleaned:
                return {"text": cleaned, "style": style}
    return None


def validate_callout(callout: Any) -> List[str]:
    """Problems with one callout dict (P0-style line-level errors)."""
    if not isinstance(callout, dict):
        return ["callout must be an object {t, text, style}"]
    errs: List[str] = []
    try:
        if float(callout.get("t", -1)) < 0:
            errs.append("callout \"t\" must be >= 0")
    except (TypeError, ValueError):
        errs.append("callout \"t\" must be a number (seconds into the shot)")
    text = callout.get("text") or ""
    if not isinstance(text, str) or not text.strip():
        errs.append("callout \"text\" must be a non-empty string")
    elif len(text) > _CALLOUT_MAX_LEN:
        errs.append(f"callout \"text\" must be at most {_CALLOUT_MAX_LEN} chars (keyword only)")
    elif text.count("\n") + 1 > _CALLOUT_MAX_LINES:
        errs.append(f"callout \"text\" must be at most {_CALLOUT_MAX_LINES} lines")
    if callout.get("style", "keyword") not in ("keyword", "number", "time", "place"):
        errs.append("callout \"style\" must be keyword | number | time | place")
    return errs


# ------------------------------------------------------------------ chapter cards

CHAPTER_CARD_MIN_DURATION = 360.0   # cards only for videos >= 6 min
CHAPTER_CARD_EVERY = 360.0          # ... then one every ~6 min at a shot boundary
CHAPTER_CARD_DUR = 1.75             # on screen 1.5-2s


def _card_title(sentence: str, index: int) -> str:
    """Stakes-based card title from the boundary sentence: its first clause,
    title-cased, max 40 chars. Auto-generated; a reviewer may override it in
    plan.json (chapter_cards[i].title)."""
    text = re.sub(r"\s+", " ", (sentence or "").strip()).rstrip(".!?")
    clause = re.split(r"[,;:—–]", text, maxsplit=1)[0].strip()
    words = clause.split()[:6]
    title = " ".join(words) if words else f"Part {index + 1}"
    title = title[:40].rstrip()
    return title.title() if title else f"Part {index + 1}"


def build_chapter_cards(shots: List[Dict[str, Any]], duration: float) -> List[Dict[str, Any]]:
    """Chapter cards for a plan: one at each ~6 min boundary, on a shot boundary.

    Short videos (<6 min) get no cards: a card at 0:00 would fight the hook.
    Titles are auto-generated from the boundary sentence; override in plan.json.
    """
    if duration < CHAPTER_CARD_MIN_DURATION:
        return []
    cards: List[Dict[str, Any]] = []
    boundary = CHAPTER_CARD_EVERY
    idx = 1
    for s in shots:
        try:
            start = float(s["start"])
        except (KeyError, TypeError, ValueError):
            continue
        if start >= boundary - 1e-9:
            cards.append({"t": round(start, 3), "title": _card_title(s.get("sentence") or "", idx),
                          "dur": CHAPTER_CARD_DUR})
            idx += 1
            boundary += CHAPTER_CARD_EVERY
    return cards


def chapter_cards_to_timestamps(cards: List[Dict[str, Any]]) -> List[str]:
    """YouTube description timestamps: `MM:SS Title`, one per card."""
    lines = []
    for c in cards:
        t = float(c.get("t") or 0)
        lines.append(f"{int(t // 60):02d}:{int(t % 60):02d} {c.get('title', '')}".rstrip())
    return lines


# ------------------------------------------------------------------ SFX + music

SFX_FILES = {"whoosh": "whoosh.mp3", "impact": "impact.mp3", "ding": "ding.mp3", "sting": "sting.mp3"}
# CapCut-style extras (from the intro SFX plan). Kept separate so the
# original four keep their exact resolve_sfx_files() behaviour (tests
# assert the missing set for the core four); SFX_ALL is the full set the
# planner, validator and renderer use.
SFX_EXTRA_FILES = {"scratch": "scratch.mp3", "rumble": "rumble.mp3", "hit": "hit.mp3"}
SFX_ALL = {**SFX_FILES, **SFX_EXTRA_FILES}
SFX_VOLUME = 0.5          # SFX sit below narration, never over a sentence
MUSIC_DUCKED_VOLUME = 0.22  # 15-25% under narration (sidechain ducks further under speech)
MUSIC_SOLO_VOLUME = 0.7   # target when no narration (sidechain release; see render._audio_graph)
MUSIC_FADE = 0.5          # music fade in/out, seconds (>= 0.3s)


def plan_sfx(shots: List[Dict[str, Any]], chapter_cards: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """SFX events for a plan, from shots + chapter cards. Pure / testable.

    whoosh on every chapter card; impact on every shake (hit); sting on every
    flash (reveal); ding when a shot's sentence mentions a system/status window.
    Types are keys of SFX_ALL; the renderer skips any type with no file.
    """
    events: List[Dict[str, Any]] = []
    for c in chapter_cards:
        events.append({"t": round(float(c.get("t") or 0), 3), "type": "whoosh"})
    for s in shots:
        try:
            start = float(s["start"])
        except (KeyError, TypeError, ValueError):
            continue
        for e in s.get("effects") or []:
            if e.get("type") == "shake":
                events.append({"t": round(float(e["t"]), 3), "type": "impact"})
            elif e.get("type") == "flash":
                events.append({"t": round(float(e["t"]), 3), "type": "sting"})
        text = (s.get("sentence") or "").lower()
        if "system window" in text or "status window" in text:
            events.append({"t": round(start + 0.3, 3), "type": "ding"})
        # CapCut extras, all gated so they never spam: scratch on a twist
        # reveal ("but / however / suddenly" + a flash), rumble on a long
        # reveal (>5s, a big moment), hit on an impact sentence with no
        # shake effect of its own (a shake already gets "impact").
        _has_shake = any(x.get("type") == "shake" for x in s.get("effects") or [])
        _has_flash = any(x.get("type") == "flash" for x in s.get("effects") or [])
        _dur = float(s.get("end", start)) - start
        if _has_flash and any(w in text for w in (" but ", " however ", " except ", " suddenly ")):
            events.append({"t": round(start + 0.3, 3), "type": "scratch"})
        if _has_flash and _dur > 5.0:
            events.append({"t": round(start + 0.3, 3), "type": "rumble"})
        if not _has_shake and any(w in text for w in ("slams", "crashes", "explodes", "strikes", "smashes", "blasts")):
            events.append({"t": round(start + 0.3, 3), "type": "hit"})
    events.sort(key=lambda e: (e["t"], e["type"]))
    return events


def resolve_sfx_files(root: Path, sfx_dir: str | None, kinds=None) -> tuple[Dict[str, Path], List[str]]:
    """Map SFX type -> existing file. Missing dir / files are a warning, never
    an error: the plan still renders, minus that SFX. Returns (found, missing)."""
    if not sfx_dir:
        return {}, []
    base = Path(root) / sfx_dir
    found: Dict[str, Path] = {}
    missing: List[str] = []
    _files = SFX_FILES if kinds is None else {k: SFX_ALL[k] for k in kinds if k in SFX_ALL}
    for kind, name in _files.items():
        p = base / name
        if p.is_file():
            found[kind] = p
        else:
            missing.append(kind)
    return found, missing


def resolve_music(vcfg: Dict[str, Any], root: Path | None) -> str | None:
    """Music for a plan: explicit `video.music` wins; else `music/bed.mp3` in the
    series folder is used when it exists (drop a royalty-free loop there and
    every video gets a ducked bed). None when no file exists — the render
    still works, music-free. Never invents audio."""
    explicit = (vcfg or {}).get("music")
    if explicit:
        return str(explicit)
    if root is not None:
        bed = Path(root) / "music" / "bed.mp3"
        if bed.is_file():
            return "music/bed.mp3"
    return None


# ------------------------------------------------------------------ hook / loops

def hook_status(cues_sentences: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Hook / open-loop status from cues.json sentences (P3).

    Explicit `[HOOK]` on the first sentence = hook present. Without it, the
    first sentence is the de-facto hook (info, not failure — old recaps keep
    building). Open loops are `[LOOP: ...]` markers; zero loops is a warning,
    not a failure.
    """
    sentences = cues_sentences or []
    explicit = bool(sentences and sentences[0].get("hook"))
    loops = [s.get("loops") for s in sentences if s.get("loops")]
    flat = [x for group in loops for x in (group if isinstance(group, list) else [group])]
    warning = None
    if not explicit:
        warning = "no [HOOK] marker on the first sentence — the first sentence is used as the hook; add [HOOK] to mark the proof + stakes + roadmap opening"
    elif not flat:
        warning = "no [LOOP: ...] markers — consider opening a loop every 2-3 minutes"
    return {"explicit_hook": explicit, "first_payoff_at": None,
            "open_loops": len(flat), "loops": flat[:8],
            "warning": warning}

# ------------------------------------------------------------------ beat sync

BEAT_MAX_SHIFT = 0.15   # a cut never moves more than this to land on a beat
BEAT_MIN_SHOT = 0.2     # a snapped cut never makes a shot shorter than this


def snap_cuts_to_beats(shots, beats, max_shift=BEAT_MAX_SHIFT, min_shot=BEAT_MIN_SHOT):
    """Snap interior cuts to the nearest beat, within +/-max_shift seconds.

    - The first shot still starts at 0 and the last still ends on the
      video duration: only interior boundaries move.
    - No cut moves more than max_shift off its sentence, and no shot gets
      shorter than min_shot: a snapped cut is clamped into its neighbours.
    - No beats (None / empty) is a no-op: the shots come back unchanged.
    Returns (shots, info) with moved count + max shift actually applied.
    """
    if not shots or not beats:
        return shots, {"enabled": False, "moved": 0, "max_shift": 0.0,
                       "reason": "no beats (no video.beats or music/beats.json)"}
    beats = sorted(float(b) for b in beats)
    bounds = [float(s["start"]) for s in shots] + [float(shots[-1]["end"])]
    new_bounds = [bounds[0]]
    moved = 0
    max_moved = 0.0
    for k in range(1, len(bounds) - 1):
        c = bounds[k]
        nearest = min(beats, key=lambda b: abs(b - c))
        cand = nearest if abs(nearest - c) <= max_shift else c
        lo = new_bounds[-1] + min_shot
        hi = bounds[k + 1] - min_shot
        cand = min(max(cand, lo), hi) if hi >= lo else c
        if abs(cand - c) > 1e-9:
            moved += 1
            max_moved = max(max_moved, abs(cand - c))
        new_bounds.append(cand)
    new_bounds.append(bounds[-1])
    out = []
    for k, s in enumerate(shots):
        out.append({**s, "start": round(new_bounds[k], 3), "end": round(new_bounds[k + 1], 3)})
    return out, {"enabled": True, "moved": moved, "max_shift": round(max_moved, 3),
                   "reason": None}


def resolve_beats(vcfg, root):
    """Beat times (seconds) for snap_cuts_to_beats, or None.

    Explicit `video.beats` (a list of seconds) wins; else music/beats.json
    in the series folder ({"beats": [...]}, from any beat-detection tool).
    None means no beat sync: cuts stay exactly on their sentences.
    """
    beats = (vcfg or {}).get("beats")
    if beats:
        return sorted(float(b) for b in beats)
    if root is not None:
        p = Path(root) / "music" / "beats.json"
        if p.is_file():
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                return None
            raw = data.get("beats") if isinstance(data, dict) else data
            if isinstance(raw, list):
                return sorted(float(b) for b in raw)
    return None
