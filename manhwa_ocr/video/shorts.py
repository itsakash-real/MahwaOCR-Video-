"""P5 — UX / Shorts: pick vertical Shorts from a finished plan, build the
end-screen spec, and apply reviewer overrides for chapter cards / callouts.

All helpers here are pure functions of (plan, fixes, ...) — no I/O, no audio
generation, no re-recording. The narration audio for a Short is the chapter's
own recording sliced to the Short's window (see build.make_shorts, which reuses
the clip path: ffmpeg `-ss` on the existing audio file). A Short is never given
newly generated audio.

End-screen: YouTube requires *native* end-screen cards; they cannot be burned
into the video. So the end-screen is a spec (window + slots + a WATCH NEXT
description block), exported as `video/chapter_N.endscreen.json`, never baked
into pixels.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List

SHORTS_TARGET = 35.0   # seconds per Short (30-45s window)
SHORTS_MIN = 30.0
SHORTS_MAX = 45.0
SHORTS_COUNT = 3
END_CARD_TEXT = "Full story on channel"
END_CARD_DUR = 1.75
END_SCREEN_WINDOW = 20.0  # last 5-20s of the video (YouTube's end-screen range)

_CALLOUT_MAX_LEN = 40
_CARD_TITLE_MAX_LEN = 40


# ------------------------------------------------------------------ scoring

def score_shot(shot: Dict[str, Any], index: int) -> float:
    """Impact score for one shot: hook first, then peaks.

    First shot = the hook (+3). Shake (hit) +2, flash (reveal) +2, a keyword
    callout +2, a question in the narration +0.5 (open loop). Shots under 0.5s
    are not usable anchors (-5). Deterministic: no RNG, ties break by start.
    """
    try:
        dur = float(shot.get("end", 0)) - float(shot.get("start", 0))
    except (TypeError, ValueError):
        dur = 0.0
    score = 0.0
    if index == 0:
        score += 3.0
    for e in shot.get("effects") or []:
        if e.get("type") == "shake":
            score += 2.0
        elif e.get("type") == "flash":
            score += 2.0
    if shot.get("callouts"):
        score += 2.0
    if "?" in (shot.get("sentence") or ""):
        score += 0.5
    if dur < 0.5:
        score -= 5.0
    return score


def _snap_start(shots: List[Dict[str, Any]], t: float) -> float:
    """Nearest shot start at or before t (within 2s), else t. Shorts start on
    a cut, never mid-sentence."""
    # Latest shot start <= t, if within 2s.
    cands = [float(s["start"]) for s in shots
             if isinstance(s, dict) and "start" in s and float(s["start"]) <= t + 1e-9
             and t - float(s["start"]) <= 2.0 + 1e-9]
    return max(cands) if cands else t


def pick_shorts_windows(plan: Dict[str, Any], count: int = SHORTS_COUNT) -> List[Dict[str, Any]]:
    """Up to `count` non-overlapping Shorts windows from a finished plan.

    Each window is SHORTS_TARGET seconds centred on a top-scoring shot,
    snapped to a shot boundary, clamped to [0, duration]. Windows need a >=1s
    gap from each other. A chapter shorter than SHORTS_MIN returns one window
    covering the whole chapter. Returns [{start, end, anchor_shot}] sorted by
    start. Pure / testable.
    """
    shots = [s for s in (plan.get("shots") or []) if isinstance(s, dict)]
    try:
        duration = float(plan.get("duration") or 0)
    except (TypeError, ValueError):
        duration = 0.0
    if not shots or duration <= 0:
        return []
    if duration < SHORTS_MIN:
        return [{"start": 0.0, "end": round(duration, 3),
                 "anchor_shot": shots[0].get("id") if shots else None}]
    ranked = sorted(range(len(shots)),
                    key=lambda i: (-score_shot(shots[i], i), float(shots[i].get("start") or 0)))
    picked: List[Dict[str, Any]] = []
    for i in ranked:
        if len(picked) >= count:
            break
        s = shots[i]
        try:
            centre = (float(s["start"]) + float(s["end"])) / 2.0
        except (TypeError, ValueError):
            continue
        start = max(0.0, centre - SHORTS_TARGET / 2.0)
        start = _snap_start(shots, start)
        end = min(duration, start + SHORTS_TARGET)
        if end - start < SHORTS_MIN:
            # Snapping near the end squeezed the window: shift it earlier.
            start = max(0.0, end - SHORTS_TARGET)
            end = min(duration, start + SHORTS_TARGET)
        if end - start < SHORTS_MIN - 1e-9:
            continue
        if any(not (end + 1.0 <= p["start"] or start >= p["end"] + 1.0) for p in picked):
            continue
        picked.append({"start": round(start, 3), "end": round(end, 3),
                       "anchor_shot": s.get("id")})
    picked.sort(key=lambda w: w["start"])
    return picked


def shorts_title_suggestion(plan: Dict[str, Any], window: Dict[str, Any], index: int) -> str:
    """Title suggestion for one Short: first clause of its first sentence,
    title-cased, max 40 chars. Falls back to 'Short N'."""
    for s in plan.get("shots") or []:
        try:
            if float(s["start"]) >= float(window["start"]) - 1e-9:
                text = re.sub(r"\s+", " ", (s.get("sentence") or "").strip()).rstrip(".!?")
                clause = re.split(r"[,;:—–]", text, maxsplit=1)[0].strip()
                title = " ".join(clause.split()[:6])[:_CARD_TITLE_MAX_LEN].rstrip()
                return (title.title() if title else f"Short {index}") or f"Short {index}"
        except (TypeError, ValueError):
            continue
    return f"Short {index}"


def shorts_manifest(plan: Dict[str, Any], windows: List[Dict[str, Any]] | None = None) -> Dict[str, Any]:
    """Manifest for `video/chapter_N.shorts.json`: windows + titles + panels.

    Panels are the unique panels in the window (story order). The end card is
    text-only metadata — the renderer shows it as a chapter-card-style overlay
    on the Short's last shot (see build.make_shorts), never as new audio.
    """
    if windows is None:
        windows = pick_shorts_windows(plan)
    shots = plan.get("shots") or []
    out: List[Dict[str, Any]] = []
    for i, w in enumerate(windows, start=1):
        try:
            start, end = float(w["start"]), float(w["end"])
        except (TypeError, ValueError, KeyError):
            continue
        panels: List[str] = []
        hook = ""
        for s in shots:
            try:
                st, en = float(s["start"]), float(s["end"])
            except (TypeError, ValueError):
                continue
            if en > start and st < end:
                if not hook:
                    hook = (s.get("sentence") or "")[:120]
                for c in s.get("cards") or []:
                    if c.get("panel") and c["panel"] not in panels:
                        panels.append(c["panel"])
        out.append({
            "index": i,
            "start": round(start, 3), "end": round(end, 3),
            "duration": round(end - start, 3),
            "title_suggestion": shorts_title_suggestion(plan, w, i),
            "hook": hook,
            "panels": panels,
            "format": "9:16",
            "end_card": {"text": END_CARD_TEXT, "dur": END_CARD_DUR},
            "anchor_shot": w.get("anchor_shot"),
        })
    return {"chapter": plan.get("chapter"), "duration": plan.get("duration"),
            "format": "9:16", "count": len(out), "shorts": out}


def validate_shorts_manifest(data: Any) -> List[str]:
    """Problems with a shorts manifest (P0-style errors)."""
    if not isinstance(data, dict):
        return ["shorts manifest must be a JSON object"]
    errs: List[str] = []
    shorts = data.get("shorts")
    if not isinstance(shorts, list) or not shorts:
        return ["shorts manifest needs a non-empty \"shorts\" list"]
    try:
        dur = float(data.get("duration") or 0)
    except (TypeError, ValueError):
        dur = 0.0
    for i, sh in enumerate(shorts):
        where = f"shorts[{i}]"
        if not isinstance(sh, dict):
            errs.append(f"{where} must be an object")
            continue
        try:
            st, en = float(sh["start"]), float(sh["end"])
        except (KeyError, TypeError, ValueError):
            errs.append(f"{where} needs numeric \"start\" and \"end\"")
            continue
        if not (0 <= st < en <= dur + 1e-9):
            errs.append(f"{where} window {st}-{en}s must sit inside the chapter (0-{dur:.1f}s)")
        if dur >= SHORTS_MIN and not (SHORTS_MIN - 1e-9 <= en - st <= SHORTS_MAX + 1e-9):
            errs.append(f"{where} lasts {en - st:.1f}s; Shorts should be {SHORTS_MIN:.0f}-{SHORTS_MAX:.0f}s")
        if not isinstance(sh.get("title_suggestion"), str) or not sh["title_suggestion"].strip():
            errs.append(f"{where} needs a non-empty \"title_suggestion\"")
    return errs


# ------------------------------------------------------------------ end screen

def build_end_screen(duration: float, chapter: str, series_name: str | None = None) -> Dict[str, Any]:
    """End-screen spec: last END_SCREEN_WINDOW seconds, three slots, and a
    WATCH NEXT description block. Pure / testable. Never burned into pixels:
    YouTube end-screen cards must be added natively in YouTube Studio."""
    try:
        duration = float(duration)
    except (TypeError, ValueError):
        duration = 0.0
    start = round(max(0.0, duration - END_SCREEN_WINDOW), 3)
    try:
        n = int(chapter)
        next_target = f"chapter_{n + 1}.mp4" if n >= 0 else None
        next_label = f"Chapter {n + 1}"
    except (TypeError, ValueError):
        next_target, next_label = None, "Next chapter"
    series = (series_name or "this series").strip() or "this series"
    watch_next = (
        "WATCH NEXT:\n"
        f"- {next_label}: {next_target or 'the next chapter in this series'}\n"
        f"- Full story: {series} — Full Story playlist\n"
        "- Subscribe for the next chapter recap"
    )
    return {
        "window": {"start": start, "end": round(duration, 3)},
        "slots": [
            {"type": "next_video", "label": next_label, "target": next_target},
            {"type": "playlist", "label": f"{series} — Full Story", "target": None},
            {"type": "subscribe", "label": "Subscribe", "target": None},
        ],
        "watch_next": watch_next,
        "note": "Add these as native YouTube end-screen cards in YouTube Studio; do not burn them into the video.",
    }


def validate_end_screen(data: Any) -> List[str]:
    """Problems with an end-screen spec."""
    if not isinstance(data, dict):
        return ["end-screen spec must be a JSON object"]
    errs: List[str] = []
    win = data.get("window")
    if not isinstance(win, dict):
        return ["end-screen spec needs a \"window\" {start, end}"]
    try:
        st, en = float(win["start"]), float(win["end"])
    except (KeyError, TypeError, ValueError):
        return ["end-screen window needs numeric \"start\" and \"end\""]
    if not (0 <= st < en) or en - st > END_SCREEN_WINDOW + 1e-9:
        errs.append(f"end-screen window {st}-{en}s must be inside the last {END_SCREEN_WINDOW:.0f}s of the video")
    slots = data.get("slots")
    if not isinstance(slots, list) or not slots:
        errs.append("end-screen spec needs a non-empty \"slots\" list")
    else:
        for i, slot in enumerate(slots):
            if not isinstance(slot, dict) or slot.get("type") not in ("next_video", "playlist", "subscribe"):
                errs.append(f"slots[{i}] type must be next_video | playlist | subscribe")
    if not isinstance(data.get("watch_next"), str) or "WATCH NEXT" not in data.get("watch_next", ""):
        errs.append("end-screen spec needs a \"watch_next\" description block starting with WATCH NEXT")
    return errs


# ------------------------------------------------------------------ reviewer overrides (fixes.json)

def apply_card_overrides(chapter_cards: List[Dict[str, Any]], overrides: Dict[str, str],
                         shots: List[Dict[str, Any]]) -> tuple[List[Dict[str, Any]], List[str]]:
    """Apply fixes.json chapter_cards overrides {shot_id: title}.

    The override targets the card sitting on that shot's start (within 0.02s).
    Unknown shot_ids are ignored with a warning string (never silently dropped
    without a trace, never a failure — the card keeps its auto title). Returns
    (new cards, warnings). Pure / testable.
    """
    cards = [dict(c) for c in chapter_cards]
    by_start = {}
    for s in shots:
        try:
            by_start[s.get("id")] = float(s["start"])
        except (TypeError, ValueError):
            continue
    warnings: List[str] = []
    for shot_id, title in overrides.items():
        if shot_id not in by_start:
            warnings.append(f"chapter_cards override for unknown shot {shot_id!r} ignored")
            continue
        target = by_start[shot_id]
        hit = next((c for c in cards if abs(float(c.get("t") or 0) - target) <= 0.02), None)
        if hit is None:
            warnings.append(f"chapter_cards override for shot {shot_id!r} has no card at its start; ignored")
            continue
        hit["title"] = title
    return cards, warnings


def apply_callout_overrides(shots: List[Dict[str, Any]], overrides: Dict[str, str]) -> tuple[List[Dict[str, Any]], List[str]]:
    """Apply fixes.json callouts overrides {shot_id: text}.

    Replaces that shot's callouts with one callout at shot start + 0.4s (the
    same timing the planner uses). Unknown shot_ids are ignored with a
    warning. Returns (new shots, warnings). Pure / testable.
    """
    out: List[Dict[str, Any]] = []
    warnings: List[str] = []
    by_id = {s.get("id"): s for s in shots if isinstance(s, dict)}
    for s in shots:
        ns = dict(s)
        if s.get("id") in overrides:
            try:
                start = float(s.get("start") or 0)
            except (TypeError, ValueError):
                start = 0.0
            ns["callouts"] = [{"t": round(start + 0.4, 3), "text": overrides[s["id"]],
                               "style": "keyword", "dur": 1.2}]
        out.append(ns)
    for shot_id in overrides:
        if shot_id not in by_id:
            warnings.append(f"callout override for unknown shot {shot_id!r} ignored")
    return out, warnings


def validate_override_entry(kind: str, entry: Any) -> List[str]:
    """Problems with one fixes.json chapter_cards / callouts entry."""
    if not isinstance(entry, dict):
        return [f"{kind} entries must be objects {{shot_id, title/text}}"]
    errs: List[str] = []
    if not isinstance(entry.get("shot_id"), str) or not entry["shot_id"].strip():
        errs.append(f"{kind} entries need a non-empty string \"shot_id\"")
    field = "title" if kind == "chapter_cards" else "text"
    text = entry.get(field)
    if not isinstance(text, str) or not text.strip():
        errs.append(f"{kind} entries need a non-empty string \"{field}\"")
    elif len(text) > _CALLOUT_MAX_LEN:
        errs.append(f"{kind} \"{field}\" must be at most {_CALLOUT_MAX_LEN} chars (got {len(text)})")
    elif text.count("\n") + 1 > 2:
        errs.append(f"{kind} \"{field}\" must be at most 2 lines")
    return errs
