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

import re
from pathlib import Path
from typing import Any, Dict, List

# ------------------------------------------------------------------ pacing

PACING_TARGET_MIN = 3.0   # panel hold target: 3-4s
PACING_TARGET_MAX = 4.0
PACING_TEXT_MAX = 7.0     # readable text panels may hold up to 7s
PACING_MAX_HOLD = 8.0     # never hold one frame longer than this without a crop change
PACING_SPLIT_TO = 4.0     # long shots split into pieces of at most this long

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
    must stay readable, never zoom into a postage stamp.
    """
    x0, y0, x1, y1 = (float(v) for v in box)
    w, h = x1 - x0, y1 - y0
    dx = min(w * frac, (w - 0.30) / 2)
    dy = min(h * frac, (h - 0.30) / 2)
    dx = max(0.0, dx)
    dy = max(0.0, dy)
    return [round(x0 + dx, 4), round(y0 + dy, 4), round(x1 - dx, 4), round(y1 - dy, 4)]


def split_long_shot(shot: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Split one >8s shot into <=4s pieces with a punch-in crop change per piece.

    Pure geometry: no image access. Every piece keeps the same panels, sentence
    and effects that fall inside it; each later piece's cards are inset by 8%
    per piece so the cut reads as a punch-in, not a frozen restart. Shots at or
    under PACING_MAX_HOLD, shots with no cards, and shots with no crops are
    returned unchanged. Piece ids are suffixed -a/-b/...; the first piece keeps
    the original id when there is only one piece (i.e. no split happened).
    """
    try:
        dur = float(shot["end"]) - float(shot["start"])
    except (KeyError, TypeError, ValueError):
        return [shot]
    cards = shot.get("cards") or []
    if dur <= PACING_MAX_HOLD or not cards:
        return [shot]
    n = max(2, int(-(-dur // PACING_SPLIT_TO)))  # ceil
    piece = dur / n
    out: List[Dict[str, Any]] = []
    for k in range(n):
        part = dict(shot)
        part["start"] = round(float(shot["start"]) + k * piece, 3)
        part["end"] = round(float(shot["start"]) + (k + 1) * piece, 3)
        if n > 1:
            part["id"] = f"{shot.get('id', 'S?')}-{chr(ord('a') + k)}"
        new_cards = []
        for c in cards:
            cc = dict(c)
            crop = c.get("crop")
            if crop and k > 0:
                cc["crop"] = _inset_crop(list(crop), 0.08 * k)
            # A later-arriving card keeps its arrival offset inside its piece.
            at = float(c.get("at") or 0)
            cc["at"] = round(max(0.0, at - k * piece), 3)
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


def split_long_shots(shots: List[Dict[str, Any]]) -> tuple[List[Dict[str, Any]], int]:
    """Split every over-long shot. Returns (new shots, number of shots split)."""
    out: List[Dict[str, Any]] = []
    split = 0
    for s in shots:
        parts = split_long_shot(s)
        if len(parts) > 1:
            split += 1
        out.extend(parts)
    return out, split


# ------------------------------------------------------------------ callouts

_CALLOUT_MAX_LEN = 40
_CALLOUT_MAX_LINES = 2

# Rank / skill / number patterns, most specific first. A callout is the single
# most checkable promise in the sentence: a rank, a damage number, a level.
_RANK_RE = re.compile(r"\b([A-Z]{1,3}(?:\+)?-?rank|SS|SSS|S-rank|A-rank|B-rank|C-rank|D-rank|E-rank|F-rank)\b")
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
SFX_VOLUME = 0.5          # SFX sit below narration, never over a sentence
MUSIC_DUCKED_VOLUME = 0.22  # 15-25% under narration (sidechain ducks further under speech)
MUSIC_SOLO_VOLUME = 0.7   # target when no narration (sidechain release; see render._audio_graph)
MUSIC_FADE = 0.5          # music fade in/out, seconds (>= 0.3s)


def plan_sfx(shots: List[Dict[str, Any]], chapter_cards: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """SFX events for a plan, from shots + chapter cards. Pure / testable.

    whoosh on every chapter card; impact on every shake (hit); sting on every
    flash (reveal); ding when a shot's sentence mentions a system/status window.
    Types are keys of SFX_FILES; the renderer skips any type with no file.
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
    events.sort(key=lambda e: (e["t"], e["type"]))
    return events


def resolve_sfx_files(root: Path, sfx_dir: str | None) -> tuple[Dict[str, Path], List[str]]:
    """Map SFX type -> existing file. Missing dir / files are a warning, never
    an error: the plan still renders, minus that SFX. Returns (found, missing)."""
    if not sfx_dir:
        return {}, []
    base = Path(root) / sfx_dir
    found: Dict[str, Path] = {}
    missing: List[str] = []
    for kind, name in SFX_FILES.items():
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
