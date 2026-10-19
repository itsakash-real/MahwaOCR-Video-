"""`video intro`: a short trailer (chapter 0) built from panels of several chapters.

You write the narration (recorded as audio/chapter_0.mp3) and list, for every
sentence, the one to three panels it shows in intro/intro.json:

    {"version": 1,
     "sentences": [
       {"text": "He chopped trees for a living.", "panels": ["ch1:P003-02"]},
       {"text": "Then the system woke up.", "panels": ["ch2:P010-14", "ch2:P011-01"], "beat": true}]}

A panel reference is `ch<chapter>:<panel id>` (ids are on each chapter's contact sheet).
Panels are read straight from each chapter's media/chN/panels.json and page images,
nothing is copied. Everything after that is the same machinery as a chapter video:
whisper timing (same settings), the planner (full-width cards, no white gutters) and
the renderer. Outputs: video/chapter_0.alignment.json, chapter_0.plan.json, chapter_0.mp4.

The plan is presentation only. Framing fixes go in video/chapter_0.fixes.json (use the
panel ids as written in intro.json, e.g. "ch2:P010-14"); `video intro` re-applies them
every time it rebuilds the plan.

Intro rules: a text-only (or dropped) panel is rejected; a panel appears at most once in
the whole intro; every sentence shows 1-3 panels.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any, Dict, List, Tuple

from ..util import UserError, atomic_write_text, normalize_label
from . import align_engine
from . import fixes as fixes_mod
from . import sync as sync_mod
from .frames import format_from
from .media import audio_path, video_dir
from .panels import load_panels, panel_image
from .plan import build_plan, plan_path, save_plan, validate as validate_plan

INTRO_CHAPTER = "0"
MAX_PANELS = 3
REF = re.compile(r"^ch(?P<ch>[^:\s]+):(?P<pid>\S+)$", re.IGNORECASE)
DEFAULTS = {"min_hold": 2.0, "pair_ratio": 0.5, "darken": 0.15, "backdrop": "blur",
            "beat_effects": ["shake", "flash", "fade_out"]}
SHEET_COLUMNS = 6
SHEET_PER_IMAGE = 24
THUMB_W = 190
OPENING_HIT = 0.55      # seconds into the first shot when its opening shake lands


def intro_dir(root: Path) -> Path:
    return Path(root) / "intro"


def intro_json_path(root: Path) -> Path:
    return intro_dir(root) / "intro.json"


def settings(vcfg: Dict[str, Any]) -> Dict[str, Any]:
    out = dict(DEFAULTS)
    out.update({k: v for k, v in (vcfg.get("intro") or {}).items() if v is not None})
    return out


# ------------------------------------------------------------------ chapters and panels


def _chapters_with_panels(sess) -> Dict[str, Dict[str, Any]]:
    found = {}
    for c in sess.chapters():
        c = normalize_label(c)
        if c == INTRO_CHAPTER:
            continue
        data = load_panels(sess.path, c)
        if data:
            found[c] = data
    return found


def _drawn(data: Dict[str, Any]) -> List[Dict[str, Any]]:
    return [p for p in data["panels"] if not p.get("text_only")]


def ref_id(chapter: str, pid: str) -> str:
    return f"ch{chapter}:{pid}"


# ------------------------------------------------------------------ --prepare


def prepare(sess, log=print) -> List[Path]:
    """A template intro.json plus contact sheets of every chapter's drawn panels."""
    from PIL import Image, ImageDraw

    from .panels import _font

    root = sess.path
    chapters = _chapters_with_panels(sess)
    if not chapters:
        raise UserError("No chapter has panels yet. Run  video panels --chapter N  for the chapters you want to use.")
    folder = intro_dir(root)
    sheets_dir = folder / "sheets"
    sheets_dir.mkdir(parents=True, exist_ok=True)
    written: List[Path] = []
    first = None
    for c, data in sorted(chapters.items(), key=lambda kv: _chapter_key(kv[0])):
        drawn = _drawn(data)
        if drawn and first is None:
            first = ref_id(c, drawn[0]["id"])
        for page, i in enumerate(range(0, len(drawn), SHEET_PER_IMAGE), start=1):
            batch = drawn[i:i + SHEET_PER_IMAGE]
            tiles = []
            for p in batch:
                img = panel_image(root, c, p, data.get("norm_width"), clean=False).convert("RGB")
                k = THUMB_W / img.width
                tiles.append((p["id"], img.resize((THUMB_W, max(8, min(400, int(img.height * k)))))))
            rows = (len(tiles) + SHEET_COLUMNS - 1) // SHEET_COLUMNS
            row_h = [max(t[1].height for t in tiles[r * SHEET_COLUMNS:(r + 1) * SHEET_COLUMNS]) + 26
                     for r in range(rows)]
            sheet = Image.new("RGB", (SHEET_COLUMNS * (THUMB_W + 8) + 8, sum(row_h) + 8), (30, 30, 30))
            draw = ImageDraw.Draw(sheet)
            font = _font(16)
            y = 8
            for r in range(rows):
                for col, (pid, tile) in enumerate(tiles[r * SHEET_COLUMNS:(r + 1) * SHEET_COLUMNS]):
                    x = 8 + col * (THUMB_W + 8)
                    draw.text((x, y), ref_id(c, pid), fill=(255, 220, 90), font=font)
                    sheet.paste(tile, (x, y + 22))
                y += row_h[r]
            out = sheets_dir / f"ch{c}_{page:02d}.png"
            sheet.save(out)
            written.append(out)
    template = intro_json_path(root)
    if not template.exists():
        body = {"version": 1,
                "_help": ("One entry per narrated sentence: its text and 1-3 panels (ch<chapter>:<panel id>, "
                          "ids are labelled on intro/sheets/*.png). A panel may be used once in the whole intro; "
                          "text-only panels are not allowed. \"beat\": true marks a big moment "
                          "(shake / flash / fade-out). Record the narration as audio/chapter_0.mp3."),
                "sentences": [{"text": "", "panels": [first or "ch1:P001-01"], "beat": False}]}
        atomic_write_text(template, json.dumps(body, indent=1, ensure_ascii=False))
        written.append(template)
        log(f"  wrote template {template.relative_to(root).as_posix()}")
    else:
        log(f"  kept your existing {template.relative_to(root).as_posix()}")
    for s in written:
        if s.suffix == ".png":
            log(f"  contact sheet: {s.relative_to(root).as_posix()}")
    return written


def _chapter_key(label: str):
    try:
        return (0, float(label), label)
    except ValueError:
        return (1, 0.0, label)


# ------------------------------------------------------------------ reading intro.json


def sentences_from_raw(root: Path, chapters: Dict[str, Dict[str, Any]], raw: List[Any],
                       dropped: Dict[str, set] | None = None,
                       source: str = "intro/intro.json") -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """Validate a raw sentences list (manual intro.json, or an auto-pick) into
    sentences + synthetic panels_data. Shared by the manual and auto paths so
    both get the same text-only / reuse / unknown-panel checks."""
    dropped = dropped or {}
    problems: List[str] = []
    used: Dict[str, int] = {}
    out: List[Dict[str, Any]] = []
    panels: List[Dict[str, Any]] = []
    for n, entry in enumerate(raw, start=1):
        if not isinstance(entry, dict):
            problems.append(f"sentence {n}: must be an object with \"text\" and \"panels\"")
            continue
        text = str(entry.get("text") or "").strip()
        if not text:
            problems.append(f"sentence {n}: has no text")
        refs_in = entry.get("panels")
        if not isinstance(refs_in, list) or not (1 <= len(refs_in) <= MAX_PANELS):
            problems.append(f"sentence {n}: \"panels\" must list 1 to {MAX_PANELS} panels like \"ch2:P010-14\"")
            continue
        refs: List[str] = []
        for ref in refs_in:
            m = REF.match(str(ref).strip())
            if not m:
                problems.append(f"sentence {n}: {ref!r} is not a panel reference (write it like \"ch2:P010-14\")")
                continue
            c, pid = normalize_label(m.group("ch")), m.group("pid")
            if c not in chapters:
                problems.append(f"sentence {n}: {ref}: chapter {c} has no panels (run  video panels --chapter {c})")
                continue
            panel = next((p for p in chapters[c]["panels"] if p["id"] == pid), None)
            if panel is None:
                problems.append(f"sentence {n}: {ref}: chapter {c} has no panel {pid} "
                                f"(ids are on intro/sheets/ch{c}_*.png)")
                continue
            if panel.get("text_only") or pid in dropped.get(c, set()):
                problems.append(f"sentence {n}: {ref} is a text-only panel (no drawn art); pick a drawn panel")
                continue
            rid = ref_id(c, pid)
            if rid in used:
                problems.append(f"sentence {n}: {rid} is already used in sentence {used[rid]}; "
                                "each panel appears at most once in the intro")
                continue
            used[rid] = n
            refs.append(rid)
            panels.append({**panel, "id": rid, "src_id": pid, "src_chapter": c,
                           "src_norm_w": chapters[c].get("norm_width"),
                           "src_fingerprint": chapters[c].get("fingerprint"), "text_only": False})
        out.append({"text": text, "refs": refs, "beat": bool(entry.get("beat"))})
    if problems:
        raise UserError(f"{source} has problems (nothing was built):\n  - " + "\n  - ".join(problems))
    fp = hashlib.sha256(json.dumps([[p["id"], p.get("src_fingerprint")] for p in panels]).encode()).hexdigest()[:16]
    first_chapter = panels[0]["src_chapter"]
    synthetic = {"version": 1, "chapter": INTRO_CHAPTER, "fingerprint": fp,
                 "norm_width": chapters[first_chapter].get("norm_width") or 1, "panels": panels}
    return out, synthetic


def load_intro(root: Path, chapters: Dict[str, Dict[str, Any]],
               dropped: Dict[str, set] | None = None) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """Validated sentences [{"text", "refs", "beat"}] and the synthetic chapter-0 panels_data.
    Every problem is collected, so the writer fixes the file once."""
    path = intro_json_path(root)
    if not path.exists():
        raise UserError(f"No {path.relative_to(root).as_posix()} yet. Start one with:  video intro --prepare")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise UserError(f"intro/intro.json is not valid JSON ({exc}).") from None
    raw = data.get("sentences") if isinstance(data, dict) else None
    if not isinstance(raw, list) or not raw:
        raise UserError("intro/intro.json needs a non-empty \"sentences\" list.")
    return sentences_from_raw(root, chapters, raw, dropped=dropped)


def _dropped_by_fixes(root: Path, chapters: Dict[str, Dict[str, Any]]) -> Dict[str, set]:
    """Panels a chapter's own fixes.json dropped (they are text-only for the chapter's video)."""
    out: Dict[str, set] = {}
    for c, data in chapters.items():
        if not fixes_mod.fixes_path(root, c).exists():
            continue
        dims = {p["id"]: (float(p.get("width") or 1), float(p.get("height") or 1)) for p in data["panels"]}
        try:
            out[c] = set(fixes_mod.load(root, c, dims)["drop"])
        except UserError:
            continue
    return out


# ------------------------------------------------------------------ the plan


def _segments(sentences: List[Dict[str, Any]], timed: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    segs = []
    for n, (s, t) in enumerate(zip(sentences, timed), start=1):
        segs.append({"sentence": n, "text": s["text"], "narration_type": "VISUAL_ACTION",
                     "type_reason": "listed in intro.json", "tagged_panels": list(s["refs"]),
                     "selected_panel": s["refs"][0], "selected_by_align": s["refs"][0],
                     "sequence": list(s["refs"]), "display_action": "SHOW",
                     "confidence": "intro_listed", "needs_review": False, "beat": s["beat"],
                     "start": round(float(t["start"]), 3), "end": round(float(t["end"]), 3)})
    return segs


def style_plan(plan: Dict[str, Any], segments: List[Dict[str, Any]], icfg: Dict[str, Any],
               fixes: Dict[str, Any] | None = None) -> Dict[str, Any]:
    """The intro look on top of the normal planner output: strong first shot, push-in/zoom-in
    moves, staggered pairs, shake/flash/fade-out on beats, blurred backdrop, darker grade."""
    shots = plan["shots"]
    beat_fx = list(icfg.get("beat_effects") or [])
    darken = max(0.0, min(0.6, float(icfg.get("darken") or 0.0)))
    for i, shot in enumerate(shots):
        dur = float(shot["end"]) - float(shot["start"])
        if icfg.get("backdrop") == "blur":
            shot["background"] = "blur"                   # never black bars
        if darken > 0:
            shot["darken"] = darken
        for k, card in enumerate(shot["cards"]):
            if shot["layout"] == "pair":
                if k == 0:
                    card["motion"] = "push_in"
                else:                                      # arrives on its own words, never at once
                    card["at"] = round(min(max(0.6, float(card.get("at") or 0)), max(0.0, dur - 0.3)), 3)
            else:
                card["motion"] = "push_in"
                card["enter"] = "zoom_in" if i == 0 or i % 2 == 0 else (card.get("enter") or "cut")
    first = shots[0]
    first["cards"][0]["enter"] = "zoom_in"
    first["cards"][0]["motion"] = "push_in"
    if not any(e["type"] == "shake" for e in first["effects"]):
        t = float(first["start"]) + OPENING_HIT
        if t <= float(first["end"]) - 0.25:
            first["effects"].append({"type": "shake", "t": round(t, 3)})
    for seg in segments:
        if not seg.get("beat"):
            continue
        t0 = float(seg["start"])
        shot = next((s for s in shots if float(s["start"]) - 1e-6 <= t0 < float(s["end"])), None)
        if shot is None:
            continue
        hit = min(float(shot["end"]) - 0.25, max(t0, float(shot["start"]) + OPENING_HIT))
        if hit < float(shot["start"]):
            continue
        for fx in ("shake", "flash"):
            if fx in beat_fx and not any(e["type"] == fx for e in shot["effects"]):
                shot["effects"].append({"type": fx, "t": round(hit, 3)})
        if "fade_out" in beat_fx:
            shot["exit"] = "fade_out"
    for shot in shots:      # chapter_0.fixes.json "shot" overrides always win over the intro look
        over = (fixes or {}).get("shot") or {}
        if (over.get(shot["cards"][0]["panel"]) or {}).get("background"):
            shot["background"] = over[shot["cards"][0]["panel"]]["background"]
        for card in shot["cards"]:
            if (over.get(card["panel"]) or {}).get("enter"):
                card["enter"] = over[card["panel"]]["enter"]
    plan["effects_enabled"] = {**(plan.get("effects_enabled") or {}), "shake": True, "flash": "flash" in beat_fx}
    plan["intro"] = True
    return plan


def build_intro(sess, cfg, *, sync: str = "auto", fmt: str | None = None, log=print,
                sentences: List[Dict[str, Any]] | None = None, out_chapter: str | None = None,
                audio: Path | None = None, kind: str = "intro"
                ) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """Alignment + plan for the intro. Returns (plan, panels_data). Always rebuilt from intro.json,
    the audio and video/chapter_0.fixes.json (the plan itself is never edited by hand).

    The auto paths (`video hook`, `video intro --auto`, `video previously`) pass
    `sentences` (already picked panels, [{"text", "panels": [refs], "beat"}]),
    `out_chapter` (chapter "0" for hook, "prevN" for previously) and `audio`
    (the explicit --audio file) instead of reading intro.json / chapter_0.mp3.
    Manual `video intro` passes none of those and behaves exactly as before.
    """
    root = sess.path
    vcfg = dict(cfg.video)
    icfg = settings(vcfg)
    fmt = fmt or vcfg.get("format", "16:9")
    chapter = out_chapter or INTRO_CHAPTER
    chapters = _chapters_with_panels(sess)
    if sentences is None:
        sentences, panels_data = load_intro(root, chapters, _dropped_by_fixes(root, chapters))
    else:
        sentences, panels_data = sentences_from_raw(
            root, chapters, sentences, dropped=_dropped_by_fixes(root, chapters), source="auto-pick")
    if audio is None:
        audio = audio_path(root, chapter)
    if audio is None:
        raise UserError(f"No narration for the intro. Save it as audio/chapter_{chapter}.mp3 (or .wav/.m4a) in {root}.")
    intro_timing_warnings: list = []
    timed, mode, duration = align_engine._timed_sentences(
        root, chapter, [{"text": s["text"], "panels": []} for s in sentences], vcfg, sync, log,
        sync_mod.effective_settings(vcfg, log=log), warnings_out=intro_timing_warnings,
        audio=audio)
    segments = _segments(sentences, timed)
    refs = [r for s in sentences for r in s["refs"]]
    validation = {"status": "PASS", "problems": [],
                  "checks": {"drawn_panels_only": "PASS", "each_panel_once": "PASS", "one_to_three_per_sentence": "PASS"}}
    result = {"version": 2, "chapter": chapter, "kind": kind, "audio": {"duration": round(duration, 3)},
              "sync_mode": mode, "timing_warnings": intro_timing_warnings,
              "segments": segments, "validation": validation, "review_queue": [],
              "coverage": {"art_panels": len(refs), "placed_by_order": [], "uncovered": []}}
    atomic_write_text(align_engine.alignment_path(root, chapter),
                      json.dumps(result, indent=1, ensure_ascii=False))

    dims = {p["id"]: (float(p.get("width") or 1), float(p.get("height") or 1)) for p in panels_data["panels"]}
    fixes = fixes_mod.load(root, chapter, dims)
    drop = fixes["drop"]
    if drop:
        raise UserError(f"video/chapter_{chapter}.fixes.json drops " + ", ".join(sorted(drop))
                        + ", but every intro panel must show. Remove the drop, or take the panel out of the picks.")
    from .plan import units_from_alignment

    units = units_from_alignment(segments)
    fmt_vcfg = {**vcfg, "format": fmt, "min_hold": float(icfg["min_hold"]), "pair_ratio": float(icfg["pair_ratio"]),
                "effects": {"shake": True, "punch": False, "flash": "flash" in icfg["beat_effects"],
                            "end_fade": True}}
    try:
        rel_audio = audio.relative_to(root).as_posix()
    except ValueError:
        rel_audio = audio.as_posix()
    plan = build_plan(chapter=chapter, panels_data=panels_data, duration=duration, audio=rel_audio,
                      vcfg=fmt_vcfg, alignment_units=units,
                      alignment_meta={"sync_mode": mode, "review_queue": []},
                      image_for=lambda p: panel_image(root, p["src_chapter"], p, p["src_norm_w"], clean=True),
                      fixes=fixes)
    style_plan(plan, segments, icfg, fixes)
    shown = {c["panel"] for s in plan["shots"] for c in s["cards"]}
    missing = [r for r in refs if r not in shown]
    if missing:
        raise UserError("The narration is too short to give every panel its minimum time "
                        f"({icfg['min_hold']:g}s). Not shown: {', '.join(missing)}. "
                        "Use fewer panels or a longer narration.")
    errs = validate_plan(plan, set(dims))
    if errs:
        raise UserError("Internal error: the intro plan is invalid:\n  " + "\n  ".join(errs[:5]))
    plan["generated_hash"] = hashlib.sha256(json.dumps(plan["shots"], sort_keys=True).encode()).hexdigest()[:16]
    save_plan(root, chapter, plan)
    log(f"  {kind}: {len(plan['shots'])} shots, {len(refs)} panels, {mode} timing, {duration:.1f}s")
    log(f"  wrote {plan_path(root, chapter).relative_to(root).as_posix()}")
    return plan, panels_data


def run(sess, cfg, *, sync: str = "auto", fmt: str | None = None, preview: bool = False, workers: int = 0,
        plan_only: bool = False, log=print) -> Path | None:
    plan, panels_data = build_intro(sess, cfg, sync=sync, fmt=fmt, log=log)
    if plan_only:
        return None
    from .render import render

    return render(sess.path, INTRO_CHAPTER, plan, cfg.video, preview=preview, workers=workers, log=log,
                  panels_data=panels_data)
