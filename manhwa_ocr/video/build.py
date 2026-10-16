"""Per-chapter video orchestration: status, planning, rendering, clips, compilations.

Timing quality, best first:
  synced     recap has panel tags + whisper (pywhispercpp) available: each
             shot starts when its sentence is actually spoken
  estimated  recap has panel tags, no whisper: right panels, sentence times
             estimated from text length
  even       no tags: the audio's length spread across panels

A plan someone edited by hand (or marked reviewed) is never silently replaced.
If its inputs change, the user is told and decides.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path
from typing import Any, Callable, Dict, List

from PIL import Image

from ..util import UserError, atomic_save_image, atomic_write_text, require_chapter, sort_chapters
from . import align_engine
from . import cues as cues_mod
from . import sync as sync_mod
from . import syncaudit
from .ffmpeg import audio_timing, close_encoder, media_duration, open_encoder, run, video_info
from .frames import background, format_from, shapes_for
from . import fixes as fixes_mod
from .verify import verify_chapter
from .plan import map_options
from .media import audio_path, ensure_gitignore, ensure_pages, list_pages, video_dir
from .panels import _font, build_panels, load_panels, panel_image
from .plan import PLAN_VERSION, build_plan, load_plan, plan_path, save_plan, units_from_alignment, validate
from .render import output_path, render
from .storyboard import storyboard_paths, write_review_task, write_storyboard


END_TAIL = 1.5   # seconds the video runs on after the last word


def _replace_dropped(units: List[Dict[str, Any]], drop: Dict[str, str]) -> List[Dict[str, Any]]:
    """A dropped panel's time is kept but shows the nearest panel that stays on
    screen (the one before it, or after it if it comes first). Timing unchanged."""
    if not drop or not units:
        return units
    kept = [u["panels"][0] not in drop for u in units]
    after: List[str | None] = [None] * len(units)
    cur: str | None = None
    for i in range(len(units) - 1, -1, -1):
        if kept[i]:
            cur = units[i]["panels"][0]
        after[i] = cur
    out, prev = [], None
    for i, u in enumerate(units):
        if kept[i]:
            prev = u["panels"][0]
            out.append(u)
            continue
        rep = prev or after[i]
        out.append({**u, "panels": [rep]} if rep else u)
    return out


def qa_path(root: Path, chapter: str) -> Path:
    return video_dir(root) / f"chapter_{require_chapter(chapter)}.qa.json"


def quality_report(plan: Dict[str, Any], panels: Dict[str, Any], align_cov: Dict[str, Any] | None,
                   min_hold: float) -> Dict[str, Any]:
    """Objective checks on a finished plan. `failures` stop the build; the rest
    are warnings for the QA file and the log. Nothing here judges art quality."""
    import math

    fmt = format_from(plan.get("format", "16:9"), int(plan.get("fps", 30)))
    dims = {p["id"]: (float(p.get("width") or 1), float(p.get("height") or 1)) for p in panels["panels"]}
    art = [p["id"] for p in panels["panels"] if not p.get("text_only")]
    shots = plan.get("shots") or []
    shown = {c["panel"] for s in shots for c in s.get("cards") or []}
    missing = [p for p in art if p not in shown]
    shape_bad = []
    for s in shots:
        allowed = shapes_for(s.get("layout", "single"), fmt)
        for c in s.get("cards") or []:
            pw, ph = dims.get(c["panel"], (1.0, 1.0))
            x0, y0, x1, y1 = (float(v) for v in c["crop"])
            pad = float(c.get("pad_y") or 0.0)          # blurred padding above and below the crop
            aspect = ((x1 - x0) * pw) / max(1e-9, ((y1 - y0) + 2 * pad) * ph)
            if min(abs(math.log(aspect / a)) for a in allowed) > 0.02:
                shape_bad.append(f"{s.get('id')} {c['panel']} ({aspect:.2f})")
    shortest = min((float(s["end"]) - float(s["start"]) for s in shots), default=0.0)
    cs = plan.get("crop_stats") or {}
    failures = []
    if missing:
        failures.append(f"{len(missing)} drawn panel(s) would never appear: {', '.join(missing[:12])}")
    if shape_bad:
        failures.append(f"{len(shape_bad)} card(s) are not one of the allowed shapes: {', '.join(shape_bad[:6])}")
    if cs.get("bubble"):
        failures.append(f"{len(cs['bubble'])} panel(s) would show a speech bubble or caption in their card: "
                        f"{', '.join(cs['bubble'][:8])}")
    if shots and shortest < min_hold - 0.01:
        failures.append(f"a shot lasts {shortest:.2f}s, under the {min_hold:g}s minimum")
    return {
        "chapter": plan.get("chapter"),
        "failures": failures,
        "art_panels": len(art), "shown_panels": len(set(art) & shown), "missing": missing,
        "placed_by_order": (align_cov or {}).get("placed_by_order", []),
        "text_only_excluded": [p["id"] for p in panels["panels"] if p.get("text_only")],
        "shots": len(shots), "shortest_shot": round(shortest, 2), "timing": plan.get("timing"),
        "short_shots": (plan.get("timing") or {}).get("short_shots", 0),
        "head_risk": cs.get("head_risk", []), "side_cut": cs.get("side_cut", []),
        "padded": cs.get("padded", []), "white_edge": cs.get("white_edge", []),
        "low_confidence_heads": cs.get("low_confidence_heads", []),
        "review_queue": plan.get("alignment_review_queue") or [],
        "sync": plan.get("sync"),
    }


def _mmss(seconds: float) -> str:
    return f"{int(seconds // 60)}:{seconds % 60:04.1f}"


def _shots_hash(plan: Dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(plan.get("shots"), sort_keys=True).encode()).hexdigest()[:16]


def _inputs(root: Path, chapter: str, panels_fp: str, audio: Path | None, fmt: str,
            vcfg: Dict[str, Any], duration: float) -> Dict[str, Any]:
    # alignment.json is plan.py's one source of panel-selection truth, so its
    # own hash (not cues.json's) is what tells us the plan needs rebuilding -
    # a pairs.json fix doesn't invalidate the cached plan until `video align`
    # has actually turned it into a new alignment.json.
    ap = align_engine.alignment_path(root, chapter)
    st = audio.stat() if audio else None
    knobs = {k: vcfg.get(k) for k in ("min_shot", "max_shot", "target_shot", "lead", "pair_ratio", "fps",
                                      "min_hold", "sync", "music", "music_volume", "effects")}
    # Framing options join the hash only when changed from their defaults, so plans built
    # before they existed stay current (a rebuild would re-frame every card).
    crop_opts = {k: v for k, v in map_options(vcfg).items()
                 if v != {"full_width": True, "min_width_kept": 0.9, "white_threshold": 242,
                          "black_threshold": 13, "pale_heads": True, "keep_top": 0.25}[k]}
    if crop_opts:
        knobs["crop"] = crop_opts
    return {
        "panels": panels_fp,
        "audio": [audio.name, st.st_size, int(st.st_mtime)] if st else None,
        "duration": round(duration, 2),
        "alignment": hashlib.sha256(ap.read_bytes()).hexdigest()[:16] if ap.exists() else None,
        "format": fmt,
        "settings": hashlib.sha256(json.dumps(knobs, sort_keys=True, default=str).encode()).hexdigest()[:12],
    }


# ------------------------------------------------------------------ status


def _alignment_status(root: Path, chapter: str) -> Dict[str, Any] | None:
    """Read-only peek at chapter_N.alignment.json for `video status` - never
    regenerates it (that's `video align`'s job), just reports what's there."""
    path = align_engine.alignment_path(root, chapter)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"broken": True}
    if not isinstance(data, dict):
        return {"broken": True}
    return {
        "broken": False,
        "sync_mode": data.get("sync_mode"),
        "validation_status": (data.get("validation") or {}).get("status"),
        "review_queue": len(data.get("review_queue") or []),
    }


def _sync_audit_status(root: Path, chapter: str, plan_file: Path) -> Dict[str, Any] | None:
    """Read-only peek at chapter_N.syncaudit.json for `video status`: the sync
    gate result of the last audit, and whether that audit still belongs to the
    current plan (a rebuilt plan invalidates it)."""
    path = syncaudit.audit_path(root, chapter)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"broken": True}
    if not isinstance(data, dict):
        return {"broken": True}
    fresh = plan_file.exists() and path.stat().st_mtime >= plan_file.stat().st_mtime
    gate = data.get("gate") or {}
    return {
        "broken": False,
        "fresh": fresh,
        "pass": bool(gate.get("pass")),
        "failures": [str(f) for f in (gate.get("failures") or [])],
        "wrong_ratio": (data.get("wrong_picture") or {}).get("ratio"),
    }


def chapter_status(root: Path, chapter: str, *, probe: bool = False) -> Dict[str, Any]:
    """What exists for one chapter's video. probe=True also reads audio length (slower)."""
    chapter = require_chapter(chapter)
    audio = audio_path(root, chapter)
    cues = cues_mod.load_cues(root, chapter)
    panels = load_panels(root, chapter)
    alignment = _alignment_status(root, chapter)
    plan_file = plan_path(root, chapter)
    plan = None
    if plan_file.exists():
        try:
            plan = json.loads(plan_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            plan = {"broken": True}
        if not isinstance(plan, dict):
            plan = {"broken": True}
        elif plan.get("version") != PLAN_VERSION and not plan.get("broken"):
            plan = {**plan, "outdated": True}
    video = output_path(root, chapter)
    preview = output_path(root, chapter, preview=True)
    # Outdated if the plan was edited or the narration was replaced after the render.
    up_to_date = bool(video.exists() and plan_file.exists() and not (plan or {}).get("outdated")
                      and video.stat().st_mtime >= plan_file.stat().st_mtime
                      and (audio is None or video.stat().st_mtime >= audio.stat().st_mtime))
    duration = None
    if audio and probe:
        try:
            duration = media_duration(audio)
        except UserError:
            duration = None
    elif plan and isinstance(plan, dict) and not plan.get("broken"):
        duration = plan.get("duration")
    return {
        "chapter": chapter,
        "transcribed": (root / f"chapter_{chapter}.md").exists(),
        "recap": (root / "recaps" / f"chapter_{chapter}.md").exists(),
        "tagged": cues.get("tagged_ratio") if cues else None,
        "tts": cues_mod.tts_path(root, chapter).exists(),
        "audio": audio.name if audio else None,
        "duration": duration,
        "pages": len(list_pages(root, chapter)),
        "panels": len(panels["panels"]) if panels else 0,
        "alignment": alignment,
        "plan": None if plan is None else {
            "broken": bool(plan.get("broken")),
            "outdated": bool(plan.get("outdated")),
            "sync": plan.get("sync"),
            "matched": (plan.get("sync_report") or {}).get("matched"),
            "reviewed": bool(plan.get("reviewed")),
            "edited": (not plan.get("broken")) and plan.get("generated_hash") not in (None, _shots_hash(plan)),
            "shots": len(plan.get("shots") or []),
        },
        "video": video.name if video.exists() else None,
        "video_up_to_date": up_to_date,
        "preview": preview.name if preview.exists() else None,
        "sync_audit": _sync_audit_status(root, chapter, plan_file),
        "storyboards": [p.name for p in storyboard_paths(root, chapter)],
    }


# ------------------------------------------------------------------ plan


def make_plan(sess, chapter: str, cfg, *, force: bool = False, sync: str = "auto", fmt: str | None = None,
              duration: float | None = None, pages_from: Path | None = None, log: Callable = print) -> Dict[str, Any]:
    root, chapter = sess.path, require_chapter(chapter)
    vcfg = dict(cfg.video)
    fmt = fmt or vcfg.get("format", "16:9")
    ensure_gitignore(root)
    # The numbers that decide timing, printed before any work so the plan's
    # meta can always be traced back to what the config said.
    sync_settings = sync_mod.effective_settings(vcfg, log=log)
    log(f"  chapter {chapter}: sync/hold settings: min_hold={sync_settings['min_hold']:g}s, "
        f"lead={sync_settings['lead']:g}s, max_late={sync_settings['max_late']:g}s, "
        f"min_hold_short={sync_settings['min_hold_short']:g}s, max_hold={sync_settings['max_hold']:g}s, "
        f"max_unmatched={sync_settings['max_unmatched']:g}s")

    if pages_from:
        from ..images import import_pages, list_images
        from .media import pages_dir

        found = list_images(Path(pages_from))
        if not found:
            raise UserError(f"No images found in {pages_from}")
        import_pages(found, pages_dir(root, chapter), log=log)
        log(f"  chapter {chapter}: imported {len(found)} page images")
    ensure_pages(sess, chapter, cfg.download, log=log)
    panels = build_panels(root, chapter, vcfg.get("panels"), log=log)

    audio = audio_path(root, chapter)
    if duration is None:
        if audio is None:
            raise UserError(
                f"No narration audio for chapter {chapter}. Save it as audio/chapter_{chapter}.mp3 "
                f"(or .wav/.m4a) in {root}, or upload it from the dashboard."
            )
        timing = audio_timing(audio)
        duration = timing["duration"]
        if timing["speech_end"] + END_TAIL < duration - 0.5:
            # TTS exports often pad silence on the end; the video ends with the voice.
            log(f"  chapter {chapter}: narration ends at {_mmss(timing['speech_end'])}; skipping "
                f"{duration - timing['speech_end']:.0f}s of silence at the end of {audio.name}")
            duration = timing["speech_end"] + END_TAIL
    if duration < 1.0:
        raise UserError(f"The narration for chapter {chapter} is only {duration:.2f}s long. Check {audio}.")
    rel_audio = audio.relative_to(root).as_posix() if audio else None
    inputs = _inputs(root, chapter, panels.get("fingerprint", ""), audio, fmt, vcfg, duration)
    fp = fixes_mod.fixes_path(root, chapter)
    inputs["fixes"] = hashlib.sha256(fp.read_bytes()).hexdigest()[:16] if fp.exists() else None

    existing_file = plan_path(root, chapter)
    if existing_file.exists() and not force:
        try:
            existing = json.loads(existing_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            existing = None
        if isinstance(existing, dict) and existing.get("version") != PLAN_VERSION:
            if existing.get("reviewed") or existing.get("generated_hash") != _shots_hash(existing):
                raise UserError(
                    f"Chapter {chapter}'s plan was made for the old editing style and can't be rendered. "
                    f"Rebuild it with:  video plan --chapter {chapter} --force  (its hand edits will be lost).")
            log(f"  chapter {chapter}: plan was made for the old editing style, rebuilding")
            existing = None
        if isinstance(existing, dict):
            if existing.get("inputs") == inputs:
                log(f"  chapter {chapter}: plan is up to date")
                return existing
            edited = existing.get("generated_hash") != _shots_hash(existing)
            if edited or existing.get("reviewed"):
                log(f"  ! chapter {chapter}: the audio, recap or panels changed, but the plan was edited/reviewed "
                    "by hand, so it was left alone. Rebuild it with --force (hand edits will be lost).")
                return existing

    # No recap/tags at all is a different, pre-existing case from "has tags but
    # the alignment is missing/broken": there is nothing to align, so panels
    # are just spread evenly - this isn't tag-based panel *selection*, nothing
    # is being selected. Once a chapter has tags, chapter_N.alignment.json
    # becomes the ONLY source of panel selection: it must already exist (made
    # with `video align`) and pass validation, with no fallback to reinterpreting
    # the tags here - a missing or broken alignment stops planning loudly.
    norm_w = panels["norm_width"]
    # Non-AI check of every card (verify.py), plus any reviewer fixes. Dropped
    # panels become text-only: they never count as missing, and no shot shows them.
    dims = {p["id"]: (float(p.get("width") or 1), float(p.get("height") or 1)) for p in panels["panels"]}
    fixes = fixes_mod.load(root, chapter, dims)
    fmt_obj = format_from(fmt, int(vcfg.get("fps", 30)))
    shapes = sorted({a for k in ("single", "pair", "wide") for a in shapes_for(k, fmt_obj)})
    report = verify_chapter(panels["panels"], lambda p: panel_image(root, chapter, p, norm_w, clean=True),
                            shapes, root, chapter, log=log, options=map_options(vcfg))
    drop = {d["panel"]: d["reason"] for d in report["dropped"]}
    drop.update(fixes["drop"])
    for p in panels["panels"]:
        if p["id"] in drop:
            p["text_only"] = True
    if drop:
        log(f"  chapter {chapter}: {len(drop)} panel(s) dropped (no character and no clean crop): "
            + ", ".join(sorted(drop)))
    if report["needs_eyes"]:
        log(f"  ! {len(report['needs_eyes'])} panel(s) with a character whose head may be cut: "
            + ", ".join(report["needs_eyes"][:12]))
    align_cov = None
    if not cues_mod.load_cues(root, chapter):
        units, mode_note, meta = None, "even timing (recap has no panel tags)", None
    else:
        align_path = align_engine.alignment_path(root, chapter)
        if not align_path.exists():
            raise UserError(
                f"Chapter {chapter} has no alignment yet. Run:  video align --chapter {chapter}\n"
                f"(it decides which panel shows for each sentence; `plan` only turns that into shots.)")
        try:
            alignment = json.loads(align_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise UserError(f"Chapter {chapter}'s alignment.json is unreadable ({exc}). "
                            f"Rebuild it with:  video align --chapter {chapter}") from None
        if not isinstance(alignment, dict) or not alignment.get("segments"):
            raise UserError(f"Chapter {chapter}'s alignment.json is invalid (no segments). "
                            f"Rebuild it with:  video align --chapter {chapter}")
        av = alignment.get("validation") or {}
        if av.get("status") != "PASS":
            problems = "; ".join(f"sentence {p.get('sentence')} [{p.get('check')}]: {p.get('detail')}"
                                 for p in (av.get("problems") or [])[:5])
            raise UserError(
                f"Chapter {chapter}'s alignment failed validation, so planning stopped (no silent fallback):\n"
                f"  {problems}\nFix it and rebuild with:  video align --chapter {chapter}")
        units = _replace_dropped(units_from_alignment(alignment["segments"]), drop)
        align_cov = alignment.get("coverage")
        queue = alignment.get("review_queue") or []
        meta = {"sync_mode": alignment.get("sync_mode"), "review_queue": queue}
        mode_note = (f"{len(alignment['segments'])} sentences from the alignment engine "
                    f"({alignment.get('sync_mode', '?')} timing, {len(queue)} flagged for review)")

    plan = build_plan(chapter=chapter, panels_data=panels, duration=duration, audio=rel_audio,
                      vcfg={**vcfg, "format": fmt}, alignment_units=units, alignment_meta=meta,
                      image_for=lambda p: panel_image(root, chapter, p, norm_w, clean=True), fixes=fixes,
                      settings=sync_settings)
    plan["inputs"] = inputs
    plan["generated_hash"] = _shots_hash(plan)
    errs = validate(plan, {p["id"] for p in panels["panels"]})
    if errs:
        raise UserError("Internal error: generated plan is invalid:\n  " + "\n  ".join(errs[:5]))
    qa = quality_report(plan, panels, align_cov,
                        min(sync_settings["min_hold"], sync_settings["min_hold_short"]))
    atomic_write_text(qa_path(root, chapter), json.dumps(qa, indent=1, ensure_ascii=False))
    if qa["failures"]:
        raise UserError(f"Chapter {chapter} can't be built yet:\n  - " + "\n  - ".join(qa["failures"])
                        + f"\nDetails: video/chapter_{chapter}.qa.json")
    save_plan(root, chapter, plan)
    log(f"  chapter {chapter}: {len(plan['shots'])} shots, {mode_note}")
    log(f"    all {qa['shown_panels']}/{qa['art_panels']} drawn panels shown; shortest shot "
        f"{qa['shortest_shot']:.1f}s; {len(qa['placed_by_order'])} placed by story order")
    if qa["head_risk"]:
        log(f"    ! {len(qa['head_risk'])} panel(s) where a head may be cut: {', '.join(qa['head_risk'][:8])}")
    if qa["review_queue"]:
        log(f"    ! {len(qa['review_queue'])} sentence(s) flagged for review (non-blocking; "
            f"see video/chapter_{chapter}.qa.json)")
    write_storyboard(root, chapter, plan, log=log)
    write_review_task(root, chapter, plan, sess.slug)
    return plan


def render_chapter(sess, chapter: str, cfg, *, preview: bool = False, workers: int = 0,
                   allow_estimated: bool = False, allow_unreviewed: bool = False, strict: bool = False,
                   allow_sync_fail: bool = False, log: Callable = print) -> Path:
    root, chapter = sess.path, require_chapter(chapter)
    panels = load_panels(root, chapter)
    if not panels:
        raise UserError(f"No panels for chapter {chapter} yet. Run:  video plan --chapter {chapter}")
    plan = load_plan(root, chapter, {p["id"] for p in panels["panels"]})
    if (cfg.video or {}).get("require_review"):          # --force is not a bypass for the review
        from .review import is_approved

        if not is_approved(root, chapter, plan):
            raise UserError(
                f"Chapter {chapter} video has not been reviewed yet (require_review is on in manhwa-ocr.yaml).\n"
                f'Review it first:  video review --chapter {chapter} --session "{sess.slug}"\n'
                f"or check the storyboard yourself and run:  video approve --chapter {chapter}")
    if plan.get("sync") == "estimated" and not allow_estimated:
        raise UserError(
            f"Chapter {chapter}'s timing was only estimated from text length, not synced to the real "
            f"narration (whisper couldn't transcribe it) — the pictures will drift out of sync with the voice.\n"
            f"Fix the sync first: lower `whisper_model` in manhwa-ocr.yaml (e.g. to base.en or tiny.en if your "
            f"machine is low on RAM), then rebuild the plan:  video plan --chapter {chapter} --force\n"
            f"To render with the estimated timing anyway, pass --force to render.")
    if plan.get("sync") == "synced":
        # The sync gate: the audit samples the plan against the narration and
        # fails on wrong-picture time and late cuts. It runs here, on the plan
        # about to be rendered, and its report stays beside it for status.
        report = syncaudit.make_report(root, chapter, cfg, log=log)
        gate = report["gate"]
        if gate["failures"]:
            detail = "; ".join(gate["failures"])
            if not allow_sync_fail:
                raise UserError(
                    f"Chapter {chapter}'s pictures don't match the narration closely enough to render "
                    f"(sync gate): {detail}.\n"
                    f"See the worst stretches:  video sync-audit --chapter {chapter}\n"
                    f"If these thresholds are wrong for this series, adjust video.sync.gate_wrong, "
                    f"gate_window or gate_grace in manhwa-ocr.yaml.\n"
                    f"To render anyway, pass --force to render.")
            log(f"  ! sync gate failed ({detail}); rendering anyway (--force)")
        else:
            worst = ((report.get("five_second") or {}).get("worst") or {}).get("wrong")
            worst_s = "none" if worst is None else f"{worst:.0%}"
            log(f"  sync gate: PASS (wrong-picture {report['wrong_picture']['ratio']:.1%} of judged time, "
                f"worst 5s window {worst_s})")
    queue = plan.get("alignment_review_queue") or []
    if queue and not strict:
        log(f"  ! {len(queue)} sentence(s) flagged for review; rendering anyway "
            f"(see video/chapter_{chapter}.qa.json). Use --strict to stop on them.")
    if queue and strict and not allow_unreviewed:
        shown = ", ".join(str(n) for n in queue[:15]) + ("..." if len(queue) > 15 else "")
        raise UserError(
            f"Chapter {chapter} has {len(queue)} sentence(s) `video align` couldn't resolve with full "
            f"confidence, never reviewed (sentence numbers: {shown}) — one of these may be the wrong "
            f"picture for its sentence.\n"
            f"Look at them first: open .work/video/ch{chapter}/pairing_NN.png for those sentence numbers, "
            f"or run `video pair --chapter {chapter}`. Fix pairs.json if anything's wrong, "
            f"then `video align --chapter {chapter}` and `video plan --chapter {chapter} --force` again.\n"
            f"To render anyway, pass --force to render.")
    if plan.get("panels_fingerprint") and plan["panels_fingerprint"] != panels.get("fingerprint"):
        log("  ! panels were re-detected since this plan was made; shots still reference valid ids, rendering anyway")
    return render(root, chapter, plan, cfg.video, preview=preview, workers=workers, log=log)


def build(sess, chapters: List[str], cfg, *, force: bool = False, sync: str = "auto", fmt: str | None = None,
          preview: bool = False, workers: int = 0, log: Callable = print) -> Dict[str, str]:
    """Plan + storyboard + render for each chapter, skipping what's already current."""
    results: Dict[str, str] = {}
    for chapter in sort_chapters(chapters):
        log(f"=== Chapter {chapter} ===")
        if audio_path(sess.path, chapter) is None:
            results[chapter] = "skipped: no audio"
            log(f"  no audio yet (save it as audio/chapter_{chapter}.mp3)")
            continue
        try:
            if cues_mod.load_cues(sess.path, chapter):
                align_engine.build_alignment(sess.path, chapter, cfg, sync=sync, log=log)
            make_plan(sess, chapter, cfg, force=force, sync=sync, fmt=fmt, log=log)
            out = output_path(sess.path, chapter, preview)
            pf = plan_path(sess.path, chapter)
            if out.exists() and out.stat().st_mtime >= pf.stat().st_mtime and not force:
                results[chapter] = f"up to date: {out.name}"
                log(f"  {out.name} is up to date")
                continue
            path = render_chapter(sess, chapter, cfg, preview=preview, workers=workers,
                                  allow_estimated=force, allow_unreviewed=force,
                                  allow_sync_fail=force, log=log)
            results[chapter] = f"rendered: {path.name}"
        except UserError as exc:
            results[chapter] = f"failed: {exc}"
            log(f"  FAILED: {exc}")
    return results


# ------------------------------------------------------------------ clips (Shorts)


def make_clip(sess, chapter: str, cfg, start: float, end: float, *, fmt: str | None = None,
              preview: bool = False, workers: int = 0, log: Callable = print) -> Path:
    """Cut a highlight from a chapter's plan, optionally re-framed as 9:16."""
    root, chapter = sess.path, require_chapter(chapter)
    panels = load_panels(root, chapter)
    if not panels:
        raise UserError(f"Plan the chapter first:  video plan --chapter {chapter}")
    plan = load_plan(root, chapter, {p["id"] for p in panels["panels"]})
    duration = float(plan["duration"])
    if not 0 <= start < end <= duration + 0.01:
        raise UserError(f"Clip range {start}-{end}s must be inside the chapter (0-{duration:.1f}s).")
    fmt = fmt or plan.get("format", "16:9")
    if fmt in {"9:16", "shorts"} and end - start > 180:
        log("  ! YouTube Shorts are limited to 3 minutes; this clip is longer.")
    shots = []
    for s in plan["shots"]:
        a, b = max(float(s["start"]), start), min(float(s["end"]), end)
        if b - a < 0.2:
            continue
        cut_in = a - float(s["start"])  # how much of this shot's opening the clip skips
        cards = []
        for c in s.get("cards") or []:
            at = float(c.get("at") or 0) - cut_in
            # A card that already arrived before the clip starts is simply there:
            # replaying its entrance would look like a glitch.
            cards.append({**c, "at": round(max(0.0, at), 3), "enter": c.get("enter") if at >= 0 else "cut"})
        extra: Dict[str, Any] = {}
        if len(cards) >= 2:
            # Where the pair's slide really happens, relative to the clipped shot
            # (negative = already happened, so the first card starts in place).
            extra["pair_move_at"] = round(float(s["cards"][1].get("at") or 0) - cut_in, 3)
        if cards and len(cards) >= 2 and float(cards[1]["at"]) > b - a - 0.25:
            cards, extra = cards[:1], {}
        layout = s.get("layout") if len(cards) >= 2 or s.get("layout") != "pair" else "single"
        shots.append({**s, **extra, "layout": layout, "cards": cards,
                      "start": round(a - start, 3), "end": round(b - start, 3),
                      "effects": [{**e, "t": round(float(e["t"]) - start, 3)} for e in s.get("effects") or []
                                  if a <= float(e["t"]) <= b]})
    if not shots:
        raise UserError("No shots fall inside that range.")
    shots[0]["start"] = 0.0
    shots[-1]["end"] = round(end - start, 3)
    for prev, cur in zip(shots, shots[1:]):
        cur["start"] = prev["end"]
    clip = {**plan, "format": fmt, "shots": shots, "duration": round(end - start, 3), "audio_offset": start}
    tag = f"clip_{start:g}-{end:g}".replace(".", "_") + ("_9x16" if fmt in {"9:16", "shorts"} else "")
    return render(root, chapter, clip, cfg.video, preview=preview, workers=workers, log=log, name=tag)


# ------------------------------------------------------------------ compilations


def _title_card(root: Path, chapter: str, fmt_name: str, out: Path, seconds: float = 2.5) -> None:
    fmt = format_from(fmt_name, 30)
    panels = load_panels(root, chapter)
    if panels:
        img = panel_image(root, chapter, panels["panels"][0], panels["norm_width"])
    else:
        img = Image.new("RGB", (900, 900), (30, 30, 40))
    frame = Image.fromarray(background("blur", img, fmt))
    from PIL import ImageDraw

    draw = ImageDraw.Draw(frame)
    font = _font(int(110 * fmt.unit))
    text = f"Chapter {chapter}"
    box = draw.textbbox((0, 0), text, font=font)
    w, h = box[2] - box[0], box[3] - box[1]
    draw.text(((fmt.width - w) / 2, (fmt.height - h) / 2), text, fill=(255, 255, 255), font=font,
              stroke_width=int(4 * fmt.unit), stroke_fill=(0, 0, 0))
    silent = out.with_name(out.stem + ".v.mp4")
    enc = open_encoder(silent, fmt.width, fmt.height, fmt.fps, crf=20, preset="veryfast")
    raw = frame.tobytes()
    for _ in range(round(seconds * fmt.fps)):
        enc.stdin.write(raw)
    close_encoder(enc)
    run(["-i", str(silent), "-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo", "-map", "0:v", "-map", "1:a",
         "-c:v", "copy", "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-ac", "2", "-shortest", str(out)])
    silent.unlink(missing_ok=True)


def compile_videos(sess, chapters: List[str], *, titles: bool = True, name: str | None = None,
                   log: Callable = print) -> Path:
    """Join finished chapter videos, with a title card before each chapter."""
    from ..script import _spec_name
    from ..workspace import work_root

    root = sess.path
    chapters = sort_chapters(require_chapter(c) for c in chapters)
    missing = [c for c in chapters if not output_path(root, c).exists()]
    if missing:
        raise UserError(f"No rendered video for chapter(s) {', '.join(missing)}. Run:  video build --chapters "
                        + ",".join(missing))
    infos = {c: video_info(output_path(root, c)) for c in chapters}
    sizes = {(i["width"], i["height"]) for i in infos.values()}
    if len(sizes) > 1:
        raise UserError("These chapters were rendered in different formats (16:9 vs 9:16); compile one format at a time.")
    fmt_name = "9:16" if next(iter(sizes))[1] > next(iter(sizes))[0] else "16:9"

    work = work_root(root) / "video" / "compile"
    work.mkdir(parents=True, exist_ok=True)
    parts: List[Path] = []
    for c in chapters:
        if titles:
            card = work / f"title_{c}_{fmt_name.replace(':', 'x')}.mp4"
            if not card.exists():
                _title_card(root, c, fmt_name, card)
            parts.append(card)
        parts.append(output_path(root, c))
    listing = work / "concat.txt"
    # Relative to the list file, so the series folder's name (which may be
    # Korean, Hindi, ...) never appears in it; quotes escaped the concat way.
    lines = []
    for p in parts:
        rel = os.path.relpath(p, work).replace("\\", "/").replace("'", "'\\''")
        lines.append(f"file '{rel}'\n")
    listing.write_text("".join(lines), encoding="utf-8")
    out = video_dir(root) / f"compiled_{name or _spec_name(chapters)}.mp4"
    tmp = out.with_name(out.stem + ".part.mp4")
    run(["-f", "concat", "-safe", "0", "-i", str(listing), "-c", "copy", "-movflags", "+faststart", str(tmp)],
        timeout=1800)
    os.replace(tmp, out)
    log(f"  wrote video/{out.name}")
    return out
