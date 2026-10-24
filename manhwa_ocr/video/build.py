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
from typing import Any, Callable, Dict, List, Tuple

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
from .plan import map_options, panelfit_advance_slice
from .media import audio_path, ensure_gitignore, ensure_pages, list_pages, video_dir
from .panels import _font, build_panels, load_panels, panel_image
from .plan import (FF_ASPECT, FF_ASPECT_TOL, FF_CONTAIN_TOL, PLAN_VERSION, build_plan, load_plan,
                    plan_path, save_plan, units_from_alignment, validate)
from .render import output_path, render
from .storyboard import storyboard_paths, write_review_task, write_storyboard


def _shape_label(value: Any) -> str:
    """Layout-switch shapes are stored as aspect floats or shape names."""
    return f"{value:g}" if isinstance(value, (int, float)) else str(value)


END_TAIL = 1.5   # seconds the video runs on after the last word


def _replace_dropped(units: List[Dict[str, Any]], drop: Dict[str, str]
                     ) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """A dropped panel's time is kept but shows the nearest panel that stays on
    screen (the one before it, or after it if it comes first). Timing unchanged.

    Also returns the substitutions made ({"sentence", "dropped", "shown", "reason"}),
    so the caller can log them loudly instead of a plan that looks fine until the
    sync gate fails that sentence later (see make_plan)."""
    if not drop or not units:
        return units, []
    kept = [u["panels"][0] not in drop for u in units]
    after: List[str | None] = [None] * len(units)
    cur: str | None = None
    for i in range(len(units) - 1, -1, -1):
        if kept[i]:
            cur = units[i]["panels"][0]
        after[i] = cur
    out, prev, subs = [], None, []
    for i, u in enumerate(units):
        if kept[i]:
            prev = u["panels"][0]
            out.append(u)
            continue
        rep = prev or after[i]
        dropped_id = u["panels"][0]
        if rep:
            subs.append({"sentence": (u.get("sentence") or "")[:80], "dropped": dropped_id,
                        "shown": rep, "reason": drop.get(dropped_id, "")})
        out.append({**u, "panels": [rep]} if rep else u)
    return out, subs


def qa_path(root: Path, chapter: str) -> Path:
    return video_dir(root) / f"chapter_{require_chapter(chapter)}.qa.json"


def quality_report(plan: Dict[str, Any], panels: Dict[str, Any], align_cov: Dict[str, Any] | None,
                   min_hold: float) -> Dict[str, Any]:
    """Objective checks on a finished plan. `failures` stop the build; the rest
    are warnings for the QA file and the log. Nothing here judges art quality."""
    import math

    from .plan import PF_ASPECT_TOL, PF_DRIFT_HEADROOM, PF_DRIFT_MAX_TRAVEL, PF_FIT_TOL

    fmt = format_from(plan.get("format", "16:9"), int(plan.get("fps", 30)))
    dims = {p["id"]: (float(p.get("width") or 1), float(p.get("height") or 1)) for p in panels["panels"]}
    art = [p["id"] for p in panels["panels"] if not p.get("text_only")]
    shots = plan.get("shots") or []
    shown = {c["panel"] for s in shots for c in s.get("cards") or []}
    missing = [p for p in art if p not in shown]
    shape_bad = []
    pf_bad = []
    placed_bad = []
    for s in shots:
        allowed = shapes_for(s.get("layout", "single"), fmt)
        for c in s.get("cards") or []:
            pw, ph = dims.get(c["panel"], (1.0, 1.0))
            if isinstance(c.get("slice"), list) and len(c["slice"]) == 2:
                # B1f: a slice card's spec is built on the BAND's
                # dimensions (panel width x band height), not the whole
                # panel's — judge its aspect against those.
                ph = ph * max(0.0, float(c["slice"][1]) - float(c["slice"][0]))
            _pl = c.get("placed")
            if isinstance(_pl, dict):
                # Placed cards are not classic shapes either: the
                # foreground is the WHOLE panel at its native aspect,
                # fitted at the MAXIMUM fit-inside scale (never
                # smaller — no floating), centred, over the panel's
                # own blurred background (flag on exactly when the
                # panel isn't canvas-shaped).
                # The unit is the EDGE-TRIMMED panel (the spec's trim
                # rect, unit fractions, applied on top of any slice
                # band above): fit, touches and the background flag
                # are all judged on the trimmed dimensions, so the
                # foreground this check reconstructs from the plan
                # alone is the art bounds, never the baked-in margins.
                _trim = _pl.get("trim")
                if isinstance(_trim, list) and len(_trim) == 4:
                    try:
                        _tw = float(_trim[2]) - float(_trim[0])
                        _th = float(_trim[3]) - float(_trim[1])
                        if 0.0 < _tw <= 1.0 and 0.0 < _th <= 1.0:
                            pw, ph = pw * _tw, ph * _th
                    except (TypeError, ValueError):
                        pass
                try:
                    _bw, _bh = float(_pl["base_w"]), float(_pl["base_h"])
                    _ok = abs(math.log((_bw / _bh) / (pw / ph))) <= PF_ASPECT_TOL
                    _scale = min(fmt.width / pw, fmt.height / ph)
                    _ok = _ok and abs(_bw - pw * _scale) <= PF_FIT_TOL \
                        and abs(_bh - ph * _scale) <= PF_FIT_TOL
                    _want_touches = ("both" if abs(_bh - fmt.height) <= PF_FIT_TOL
                                     and abs(_bw - fmt.width) <= PF_FIT_TOL
                                     else "top_bottom" if abs(_bh - fmt.height) <= PF_FIT_TOL
                                     else "left_right")
                    _ok = _ok and _pl.get("touches") == _want_touches
                    _want_bg = abs(math.log((pw / ph) / (fmt.width / fmt.height))) > PF_ASPECT_TOL
                    _ok = _ok and bool(_pl.get("bg")) == _want_bg
                except (KeyError, TypeError, ValueError, ZeroDivisionError):
                    _ok = False
                if not _ok:
                    placed_bad.append(f"{s.get('id')} {c['panel']}")
                continue
            _pf = c.get("panelfit")
            if isinstance(_pf, dict):
                # Panel-fit cards are not classic shapes: the foreground
                # is the WHOLE panel at its native aspect (never
                # stretched), fitted inside the frame — or, for a strip,
                # fitted by width and taller than the frame (it
                # scrolls). The blurred-background flag must be on
                # exactly when the panel isn't canvas-shaped.
                try:
                    _bw, _bh = float(_pf["base_w"]), float(_pf["base_h"])
                    _ok = abs(math.log((_bw / _bh) / (pw / ph))) <= PF_ASPECT_TOL
                    _kind = _pf.get("kind")
                    if _kind in ("scroll", "traverse"):
                        # Width-fit strip taller than the frame (legacy
                        # "scroll", or B1e's deliberate traverse).
                        _ok = _ok and _bh > fmt.height + PF_FIT_TOL and _bw <= fmt.width + PF_FIT_TOL
                    elif _kind == "duo":
                        # B1e side-by-side: the panel lives in its half
                        # cell — never wider than the cell, and no
                        # taller than the slide oversize band (a
                        # zoom-out duo sits inside the frame entirely).
                        _max_over = 1.0 + PF_DRIFT_MAX_TRAVEL + PF_DRIFT_HEADROOM
                        _ok = _ok and _bw <= fmt.width / 2.0 + PF_FIT_TOL \
                            and _bh <= fmt.height * _max_over + PF_FIT_TOL
                    elif _kind == "drift":
                        # Height-fit pillar oversized by (travel +
                        # headroom): never wider than the frame, and the
                        # oversize stays inside the measured band (a
                        # max-upscale-capped pillar may sit shorter).
                        _max_over = 1.0 + PF_DRIFT_MAX_TRAVEL + PF_DRIFT_HEADROOM
                        _ok = _ok and _bw <= fmt.width + PF_FIT_TOL \
                            and _bh <= fmt.height * _max_over + PF_FIT_TOL
                    elif _kind == "fill":
                        # Cover-fill: at least frame-sized both ways,
                        # with one dimension exactly the frame's.
                        _ok = _ok and _bw >= fmt.width - PF_FIT_TOL and _bh >= fmt.height - PF_FIT_TOL \
                            and min(abs(_bw - fmt.width), abs(_bh - fmt.height)) <= PF_FIT_TOL
                    else:  # zoom: the whole panel fitted inside the frame
                        _ok = _ok and _bw <= fmt.width + PF_FIT_TOL and _bh <= fmt.height + PF_FIT_TOL
                    _want_bg = abs(math.log((pw / ph) / (fmt.width / fmt.height))) > PF_ASPECT_TOL
                    if _kind == "duo":
                        _want_bg = True   # a duo always shares the blur backdrop
                    _ok = _ok and bool(_pf.get("bg")) == _want_bg
                except (KeyError, TypeError, ValueError, ZeroDivisionError):
                    _ok = False
                if not _ok:
                    pf_bad.append(f"{s.get('id')} {c['panel']}")
                continue
            x0, y0, x1, y1 = (float(v) for v in c["crop"])
            pad = float(c.get("pad_y") or 0.0)          # blurred padding above and below the crop
            aspect = ((x1 - x0) * pw) / max(1e-9, ((y1 - y0) + 2 * pad) * ph)
            if c.get("fullframe"):
                # Full-frame mode: a card's "shape" is its camera viewport
                # -- from/to must be exactly 16:9 in panel pixels and lie
                # inside the window. The crop itself is the window (any
                # aspect), so the classic shape list does not apply.
                vp = c.get("viewport") or {}
                win = vp.get("window") or [0.0, 0.0, 1.0, 1.0]
                ok = True
                for _key in ("from", "to"):
                    r = vp.get(_key) or [0.0, 0.0, 0.0, 0.0]
                    va = ((r[2] - r[0]) * pw) / max(1e-9, (r[3] - r[1]) * ph)
                    ok = ok and abs(math.log(va / FF_ASPECT)) <= FF_ASPECT_TOL
                    ok = ok and r[0] >= win[0] - FF_CONTAIN_TOL and r[1] >= win[1] - FF_CONTAIN_TOL \
                        and r[2] <= win[2] + FF_CONTAIN_TOL and r[3] <= win[3] + FF_CONTAIN_TOL
            elif c.get("wide_display"):
                # A wide-display card (video.mode: wide, or an adaptive-mode
                # card its own panel earned the wide treatment) doesn't use
                # the classic layout shapes: full frame height, width >= 6/16
                # (pairs exactly 8/16). A whole panel shown as-is is always
                # >= 6/16 wide (that's the rule that puts it there), so a
                # single aspect floor covers both windowed and whole-panel
                # singles. Checked per CARD, not per shot: adaptive mode can
                # mix a wide-style card with classic-style ones in the same
                # video (never the same shot -- pairs are never wide-style).
                ok = abs(math.log(aspect / 0.889)) <= 0.02 if s.get("layout") == "pair" else aspect >= 0.66
            else:
                ok = min(abs(math.log(aspect / a)) for a in allowed) <= 0.02
            if not ok:
                shape_bad.append(f"{s.get('id')} {c['panel']} ({aspect:.2f})")
    shortest = min((float(s["end"]) - float(s["start"]) for s in shots), default=0.0)
    cs = plan.get("crop_stats") or {}
    failures = []
    if shape_bad:
        failures.append(f"{len(shape_bad)} card(s) are not one of the allowed shapes: {', '.join(shape_bad[:6])}")
    if pf_bad:
        failures.append(f"{len(pf_bad)} card(s) break the panel-fit composition rules "
                        f"(whole panel, native aspect, blurred background): {', '.join(pf_bad[:6])}")
    if placed_bad:
        failures.append(f"{len(placed_bad)} card(s) break the placed composition rules "
                        f"(whole panel, native aspect, maximum fit-inside, own blurred background): "
                        f"{', '.join(placed_bad[:6])}")
    if cs.get("bubble"):
        failures.append(f"{len(cs['bubble'])} panel(s) would show a speech bubble or caption in their card: "
                        f"{', '.join(cs['bubble'][:8])}")
    # Wide mode's own final window (after headroom growth/containment
    # clamping, separate from the classic crop_stats check above) can still
    # be a caption box, a dominant bubble, or essentially nothing -- plan.py
    # already tried to trim it and flags it "unusable" only when trimming
    # could not leave enough real art. This must stop the build, not just
    # warn: a card like this would otherwise ship showing a blank/bubble
    # full-frame, exactly the defect this check exists to catch for good.
    if plan.get("wide_unusable"):
        failures.append(f"{len(plan['wide_unusable'])} panel(s) have no usable wide-mode window (mostly "
                        f"caption/bubble/blank, even after trimming): {', '.join(plan['wide_unusable'][:8])}")
    # A shot under the floor is the honest price of dense narration (one-word
    # sentences): it is counted in timing.short_shots and never fixed by
    # stealing time from neighbours, so only a shot too short to be seen at
    # all (under 0.2s) stops the build.
    if shots and shortest < 0.2 - 1e-6:
        failures.append(f"a shot lasts {shortest:.2f}s - too short to be seen (under 0.2s)")
    cov = align_cov or {}
    duration = float(plan.get("duration") or 0.0)
    return {
        "chapter": plan.get("chapter"),
        "failures": failures,
        "art_panels": len(art), "shown_panels": len(set(art) & shown), "missing": missing,
        # INFO ONLY: panels_shown / total is not success (and not failure).
        # Success = tagged coverage + min shot + impact pass (sync gate).
        "panels_shown": len(set(art) & shown), "panels_total": len(art),
        "panels_shown_ratio": round(len(set(art) & shown) / len(art), 4) if art else 0.0,
        "placed_by_order": cov.get("placed_by_order", []),
        "text_only_excluded": [p["id"] for p in panels["panels"] if p.get("text_only")],
        "shots": len(shots), "shortest_shot": round(shortest, 2), "timing": plan.get("timing"),
        "floor": round(float(min_hold), 3),
        "short_shots": (plan.get("timing") or {}).get("short_shots", 0),
        # QA metrics (impactful-only selection): tagged coverage per sentence
        # (from the alignment), filler shots/seconds, min shot, max shots.
        "tagged_coverage": cov.get("tagged_coverage", {}),
        "filler_shots": cov.get("filler_shots", 0),
        "filler_seconds": cov.get("filler_seconds", 0.0),
        # Wide mode only: panels whose window could not be repositioned
        # clear of a caption/text collision, or whose figure is taller than
        # any window the layout/upscale cap allows to fully contain (see
        # plan._wide_pass). INFO ONLY -- never a failure -- so a reviewer
        # sees it and can fix it by hand (fixes.json crop) instead of it
        # being a silent cut.
        "wide_caption_risk": list(plan.get("wide_caption_risk") or []),
        "wide_figure_risk": list(plan.get("wide_figure_risk") or []),
        "wide_reviewer_focal_ignored": list(plan.get("wide_reviewer_focal_ignored") or []),
        # Panel-fit mode only (plan._panelfit_pass): shots that would
        # render static (must be none — the spec forbids a still image
        # over 0.5s), panels fitted at the max-upscale cap, and the
        # impact-FX report. INFO ONLY, like the wide risks above.
        "panelfit_static": list(plan.get("panelfit_static") or []),
        "panelfit_hold": list(plan.get("panelfit_hold") or []),
        "panelfit_over_cap": list(plan.get("panelfit_over_cap") or []),
        "panelfit_impact": list(plan.get("panelfit_impact") or []),
        "panelfit_title_cards": list(plan.get("panelfit_title_cards") or []),
        # B1e: strip traverses, duo side-by-sides, zoom-out hints. INFO ONLY.
        "panelfit_traverse": list(plan.get("panelfit_traverse") or []),
        "panelfit_duo": list(plan.get("panelfit_duo") or []),
        "panelfit_zoom_out": list(plan.get("panelfit_zoom_out") or []),
        # B1f: zoom-in hints + landmark slices placed. INFO ONLY.
        "panelfit_zoom_in": list(plan.get("panelfit_zoom_in") or []),
        "panelfit_slices": list(plan.get("panelfit_slices") or []),
        # Full-frame mode only: the same honest-report treatment (see
        # plan._fullframe_pass). INFO ONLY -- never failures.
        "fullframe_caption_risk": list(plan.get("fullframe_caption_risk") or []),
        "fullframe_figure_risk": list(plan.get("fullframe_figure_risk") or []),
        "fullframe_reviewer_focal_ignored": list(plan.get("fullframe_reviewer_focal_ignored") or []),
        "fullframe_over_cap": list(plan.get("fullframe_over_cap") or []),
        "fullframe_static": list(plan.get("fullframe_static") or []),
        # Placed mode only: title cards placed + motion hints ignored.
        # INFO ONLY — composition violations are failures above.
        "placed_title_cards": list(plan.get("placed_title_cards") or []),
        "placed_hints_ignored": list(plan.get("placed_hints_ignored") or []),
        # Placed mode only: units whose baked-in margins the planner's
        # edge-trim shaved, and units placed UNTRIMMED because no art
        # bounds could be found (the no-art fallback). INFO ONLY.
        "placed_trimmed": list(plan.get("placed_trimmed") or []),
        "placed_trim_fallback": list(plan.get("placed_trim_fallback") or []),
        # Placed + Gemini Vision: units placed on a vision box, and
        # units whose vision entry fell back to the algorithmic trim.
        "placed_vision_trimmed": list(plan.get("placed_vision_trimmed") or []),
        "placed_vision_fallback": list(plan.get("placed_vision_fallback") or []),
        "min_shot": round(shortest, 2),
        "max_shots": max(0, int(duration // (1.0 if plan.get("mode") == "panelfit" else 3.0))) if duration else 0,
        "head_risk": cs.get("head_risk", []), "side_cut": cs.get("side_cut", []),
        "padded": cs.get("padded", []), "white_edge": cs.get("white_edge", []),
        "low_confidence_heads": cs.get("low_confidence_heads", []),
        # P1: mask-risk panels whose clean crop avoids the bubble (the
        # no-clean-crop ones were already dropped by verify.py).
        "mask_risk": cs.get("mask_risk", []),
        # P2: auto-switched crops (from_shape -> to_shape) and the timing
        # warnings that produced this plan — both surfaced in QA so a
        # reviewer sees what the planner changed and why timing is estimated.
        "layout_switches": cs.get("layout_switches", []),
        "timing_warnings": list(plan.get("timing_warnings") or []),
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
    if vcfg.get("mode") == "panelfit":
        # Panel-fit joins the hash only while the mode is on (the same
        # discipline as the framing options above): a panelfit plan is
        # never reused as another mode's plan, or vice versa.
        knobs["mode"] = "panelfit"
        knobs["panelfit"] = vcfg.get("panelfit") or {}
    # Full-frame mode is new (no legacy plans exist in it), so while it is
    # on, the mode + its knobs always join the hash: switching the mode or
    # a fullframe knob re-plans, exactly like a framing change should.
    if str(vcfg.get("mode") or "classic") == "fullframe":
        knobs["mode"] = "fullframe"
        knobs["fullframe"] = dict(vcfg.get("fullframe") or {})
    if str(vcfg.get("mode") or "classic") == "placed":
        knobs["mode"] = "placed"
        knobs["placed"] = dict(vcfg.get("placed") or {})
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
        # P0 safety lock: estimated-timing fallbacks surface here, so
        # `video status` shows WHY the timing is estimated, not just that
        # it is. Empty list when timing is synced.
        "timing_warnings": list(data.get("timing_warnings") or []),
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
        # P5: one-glance editor signals (P0-P3) for `video status` / `next`.
        "editor": _editor_status(root, plan, alignment),
    }


def _editor_status(root: Path, plan: Dict[str, Any] | None, alignment: Dict[str, Any] | None) -> Dict[str, Any]:
    """One-glance P0-P3 signals for `video status` / `next` / dashboard.

    Never raises: a missing/broken plan reports empty signals, not an error.
    SFX file presence is checked against the conventional `sfx/` folder
    (config `video.sfx_dir` may point elsewhere; the plan/build logs name
    the configured dir when it renders).
    """
    from .editor import resolve_sfx_files

    if not isinstance(plan, dict) or plan.get("broken"):
        return {"timing_warnings": [], "mask_risk": 0, "layout_switches": 0,
                "hook": None, "chapter_cards": 0, "callouts": 0,
                "sfx": {"events": 0, "files_present": [], "files_missing": []},
                "music": {"file": None, "present": None}, "override_warnings": []}
    cs = plan.get("crop_stats") or {}
    timing = list((alignment or {}).get("timing_warnings") or []) or list(plan.get("timing_warnings") or [])
    music_file = plan.get("music")
    found, missing = resolve_sfx_files(root, "sfx")
    return {
        "timing_warnings": [{"code": w.get("code"), "fix": w.get("fix")}
                            for w in timing if isinstance(w, dict)],
        "mask_risk": len(cs.get("mask_risk") or []),
        "layout_switches": len(cs.get("layout_switches") or []),
        "hook": plan.get("hook"),
        "chapter_cards": len(plan.get("chapter_cards") or []),
        "callouts": sum(len(s.get("callouts") or []) for s in plan.get("shots") or []),
        "sfx": {"events": len(plan.get("sfx") or []),
                "files_present": sorted(found), "files_missing": sorted(missing)},
        "music": {"file": music_file,
                  "present": (root / music_file).is_file() if music_file else None},
        "override_warnings": list(plan.get("override_warnings") or []),
    }


# ------------------------------------------------------------------ plan


def make_plan(sess, chapter: str, cfg, *, force: bool = False, sync: str = "auto", fmt: str | None = None,
              duration: float | None = None, pages_from: Path | None = None, log: Callable = print,
              storyboard: bool = True) -> Dict[str, Any]:
    root, chapter = sess.path, require_chapter(chapter)
    vcfg = dict(cfg.video)
    fmt = fmt or vcfg.get("format", "16:9")
    ensure_gitignore(root)
    # The numbers that decide timing, printed before any work so the plan's
    # meta can always be traced back to what the config said.
    sync_settings = sync_mod.effective_settings(vcfg, log=log)
    log(f"  chapter {chapter}: sync/hold settings: min_hold={sync_settings['min_hold']:g}s, "
        f"lead={sync_settings['lead']:g}s, max_late={sync_settings['max_late']:g}s, "
        f"max_hold={sync_settings['max_hold']:g}s, "
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
    if vcfg.get("mode") in ("panelfit", "placed") and not any(p.get("trim2") for p in panels.get("panels") or []):
        log(f"  chapter {chapter}: ! {vcfg.get('mode')} mode expects Rule 6 pre-processing "
            f"(video.panels.trim_v2) at panel build; these panels carry no trim2 record, "
            f"so white margins may reach the composed frames")

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
    # B1f: slice hints live in cues.json (not in the alignment the inputs
    # hash otherwise keys on), so a hint edit must invalidate a cached
    # plan on its own. Only when hints exist — hint-free chapters keep
    # byte-identical inputs.
    _peek_hints = cues_mod.slice_hints_of(cues_mod.load_cues(root, chapter))
    if _peek_hints:
        inputs["slice_hints"] = hashlib.sha256(
            json.dumps(_peek_hints, sort_keys=True).encode()).hexdigest()[:12]
    # Gemini Vision trim decisions (.work/video/ch<N>/vision_trim.json,
    # from `video vision-trim`): a changed artifact must invalidate a
    # cached plan on its own, like slice hints above. Absent artifact =
    # no key, so vision-free chapters keep identical inputs.
    from .gemini_vision import load_vision_trim, vision_trim_path

    _vision_trim = load_vision_trim(root, chapter)
    if _vision_trim is not None:
        inputs["vision_trim"] = hashlib.sha256(
            vision_trim_path(root, chapter).read_bytes()).hexdigest()[:12]

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
    layout_shapes = {layout: shapes_for(layout, fmt_obj) for layout in ("single", "pair", "wide")}
    report = verify_chapter(panels["panels"],
                            lambda p: panel_image(root, chapter, p, norm_w, clean=True,
                                                  fingerprint=panels.get("fingerprint")),
                            shapes, root, chapter, log=log, options=map_options(vcfg),
                            fingerprint=panels.get("fingerprint"),
                            layout_shapes=layout_shapes)
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
    if report.get("mask_risk"):
        log(f"  ! {len(report['mask_risk'])} panel(s) with a dominant bubble over the head range "
            f"(mask risk): {', '.join(report['mask_risk'][:12])}")
    if report.get("layout_switches"):
        log(f"  chapter {chapter}: {len(report['layout_switches'])} crop(s) will auto-switch to the pair-safe shape: "
            + ", ".join(f"{pid} {_shape_label(sw.get('from_shape'))}->{_shape_label(sw.get('to_shape'))}"
                        for pid, sw in sorted(report["layout_switches"].items())[:12]))
    if report.get("layout_sensitive"):
        log(f"  ! {len(report['layout_sensitive'])} panel(s) clean in a single shot but not in a pair "
            f"(layout-sensitive crop): {', '.join(report['layout_sensitive'][:12])}")
    if report.get("visible_patches"):
        log(f"  ! {len(report['visible_patches'])} panel(s) with a visible painted patch: "
            + ", ".join(d['panel'] for d in report['visible_patches'][:12]))
    align_cov = None
    # P3: hook / open-loop status comes from the same cues.json the plan uses.
    cues_data = cues_mod.load_cues_strict(root, chapter)
    hook_info = None
    if cues_data is not None:
        from .editor import hook_status as _hook_status
        hook_info = _hook_status(list((cues_data or {}).get("sentences") or []))
    if not cues_data:
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
        units, _dropped_subs = _replace_dropped(units_from_alignment(alignment["segments"]), drop)
        for _sub in _dropped_subs:
            log(f"  ! tagged panel {_sub['dropped']} was dropped ({_sub['reason']}) -- sentence "
                f"\"{_sub['sentence']}\" now shows {_sub['shown']} instead. Re-pair it (video pair) "
                f"if {_sub['shown']} doesn't fit, rather than discovering this at the sync gate.")
        align_cov = alignment.get("coverage")
        queue = alignment.get("review_queue") or []
        # P2: timing warnings ride with the plan (see build_plan), so status/QA/render
        # all report the same whisper outcome.
        meta = {"sync_mode": alignment.get("sync_mode"), "review_queue": queue,
                "timing_warnings": list(alignment.get("timing_warnings") or []),
                "hook": hook_info}
        mode_note = (f"{len(alignment['segments'])} sentences from the alignment engine "
                    f"({alignment.get('sync_mode', '?')} timing, {len(queue)} flagged for review)")

    # Captions are timed from the alignment segments (sentence windows),
    # never from per-shot estimates (see captions.build_captions_from_sentences).
    _caption_sentences = None
    try:
        _caption_sentences = [{"text": seg.get("text"), "start": seg.get("start"),
                               "end": seg.get("end")} for seg in alignment["segments"]]
    except (NameError, TypeError, KeyError):
        _caption_sentences = None
    plan = build_plan(chapter=chapter, panels_data=panels, duration=duration, audio=rel_audio,
                      vcfg={**vcfg, "format": fmt}, alignment_units=units, alignment_meta=meta,
                      slice_hints=cues_mod.slice_hints_of(cues_data),
                      vision_trim=_vision_trim,
                      caption_sentences=_caption_sentences,
                      image_for=lambda p: panel_image(root, chapter, p, norm_w, clean=True,
                                                      fingerprint=panels.get("fingerprint")),
                      fixes=fixes, settings=sync_settings, root=root)
    plan["inputs"] = inputs
    plan["generated_hash"] = _shots_hash(plan)
    errs = validate(plan, {p["id"] for p in panels["panels"]})
    if errs:
        raise UserError("Internal error: generated plan is invalid:\n  " + "\n  ".join(errs[:5]))
    qa = quality_report(plan, panels, align_cov, sync_settings["min_hold"])
    atomic_write_text(qa_path(root, chapter), json.dumps(qa, indent=1, ensure_ascii=False))
    if qa["failures"]:
        raise UserError(f"Chapter {chapter} can't be built yet:\n  - " + "\n  - ".join(qa["failures"])
                        + f"\nDetails: video/chapter_{chapter}.qa.json")
    save_plan(root, chapter, plan)
    # P5: end-screen spec + WATCH NEXT block ride with every plan (native
    # YouTube cards only; never burned into pixels). Override warnings are
    # logged here too, next to the other plan-time warnings.
    from .shorts import build_end_screen as _build_end_screen
    _es = _build_end_screen(float(plan["duration"]), chapter,
                            series_name=getattr(sess, "name", None) or sess.slug)
    atomic_write_text(endscreen_path(root, chapter), json.dumps(_es, indent=1, ensure_ascii=False))
    atomic_write_text(watch_next_path(root, chapter), _es["watch_next"] + "\n")
    log(f"  chapter {chapter}: {len(plan['shots'])} shots, {mode_note}")
    for _w in plan.get("override_warnings") or []:
        log(f"    ! override ignored: {_w}")
    short_note = (f"; {qa['short_shots']} shot(s) under the {qa['floor']:g}s floor (dense narration, "
                  f"left honest instead of stealing time)") if qa["short_shots"] else ""
    log(f"    panels shown {qa['shown_panels']}/{qa['art_panels']} (info only — selection, not coverage); "
        f"shortest shot {qa['shortest_shot']:.1f}s; {len(qa['placed_by_order'])} impactful filler(s){short_note}")
    if qa["head_risk"]:
        log(f"    ! {len(qa['head_risk'])} panel(s) where a head may be cut: {', '.join(qa['head_risk'][:8])}")
    if qa.get("wide_figure_risk"):
        log(f"    ! wide mode: {len(qa['wide_figure_risk'])} panel(s) whose figure is taller than any "
            f"window the upscale cap allows to fully contain: {', '.join(qa['wide_figure_risk'][:8])}")
    if qa.get("wide_caption_risk"):
        log(f"    ! wide mode: {len(qa['wide_caption_risk'])} panel(s) where the overlaid caption may "
            f"cover panel text: {', '.join(qa['wide_caption_risk'][:8])}")
    if qa.get("wide_reviewer_focal_ignored"):
        log(f"    ! wide mode: {len(qa['wide_reviewer_focal_ignored'])} panel(s) where a fixes.json focal "
            f"crop was not used as-is (panel shown whole, or the focal doesn't fit): "
            f"{', '.join(qa['wide_reviewer_focal_ignored'][:8])}")
    if qa.get("panelfit_static"):
        log(f"    ! panelfit mode: {len(qa['panelfit_static'])} shot(s) would render static "
            f"(the spec forbids a still image over 0.5s): {', '.join(qa['panelfit_static'][:8])}")
    if qa.get("panelfit_hold"):
        log(f"    panelfit mode: {len(qa['panelfit_hold'])} sub-shot(s) sit in a travel-cap hold "
            f"(their sentence's drift/zoom reached the measured cap and holds, per the reference): "
            f"{', '.join(qa['panelfit_hold'][:8])}")
    if qa.get("panelfit_over_cap"):
        log(f"    ! panelfit mode: {len(qa['panelfit_over_cap'])} panel(s) fitted at the max-upscale "
            f"cap (shown smaller, never stretched): {', '.join(qa['panelfit_over_cap'][:8])}")
    if qa.get("panelfit_impact"):
        log(f"    panelfit mode: impact fast zoom (+8.8%/s; no shake/flash in this mode) on "
            f"{len(qa['panelfit_impact'])} shot(s)")
    if qa.get("panelfit_zoom_in"):
        log(f"    panelfit mode: zoom-in hint (standard +5%/s push-in) on "
            f"{len(qa['panelfit_zoom_in'])} shot(s)")
    if qa.get("panelfit_slices"):
        log(f"    panelfit mode: {len(qa['panelfit_slices'])} landmark slice(s) placed "
            f"from cues.json slice_hints: {', '.join(qa['panelfit_slices'][:8])}")
    if qa.get("panelfit_title_cards"):
        log(f"    panelfit mode: {len(qa['panelfit_title_cards'])} title card(s) "
            f"(split-flap, on timeline cues)")
    if qa.get("fullframe_figure_risk"):
        log(f"    ! fullframe mode: {len(qa['fullframe_figure_risk'])} panel(s) whose focal cannot fit "
            f"inside one 16:9 viewport (the camera path traverses it instead): "
            f"{', '.join(qa['fullframe_figure_risk'][:8])}")
    if qa.get("fullframe_caption_risk"):
        log(f"    ! fullframe mode: {len(qa['fullframe_caption_risk'])} panel(s) where the overlaid caption "
            f"may cover panel text: {', '.join(qa['fullframe_caption_risk'][:8])}")
    if qa.get("fullframe_over_cap"):
        log(f"    ! fullframe mode: {len(qa['fullframe_over_cap'])} panel(s) below the upscale floor "
            f"(panel too small to supply a bigger viewport): {', '.join(qa['fullframe_over_cap'][:8])}")
    if qa.get("fullframe_static"):
        log(f"    ! fullframe mode: {len(qa['fullframe_static'])} shot(s) with no camera motion possible "
            f"on the available pixels: {', '.join(qa['fullframe_static'][:8])}")
    if qa.get("fullframe_reviewer_focal_ignored"):
        log(f"    ! fullframe mode: {len(qa['fullframe_reviewer_focal_ignored'])} panel(s) where a fixes.json "
            f"focal overflows every 16:9 viewport: {', '.join(qa['fullframe_reviewer_focal_ignored'][:8])}")
    if qa.get("placed_title_cards"):
        log(f"    placed mode: {len(qa['placed_title_cards'])} title card(s) "
            f"(split-flap, on timeline cues)")
    if qa.get("placed_hints_ignored"):
        log(f"    placed mode: motion hint(s) on {len(qa['placed_hints_ignored'])} shot(s) ignored "
            f"(this mode has no motion): {', '.join(qa['placed_hints_ignored'][:8])}")
    if qa.get("placed_trimmed"):
        log(f"    placed mode: {len(qa['placed_trimmed'])} unit(s) edge-trimmed in prep "
            f"(baked-in panel margins shaved before fit): {', '.join(qa['placed_trimmed'][:8])}")
    if qa.get("placed_trim_fallback"):
        log(f"    ! placed mode: {len(qa['placed_trim_fallback'])} unit(s) placed UNTRIMMED "
            f"(no art bounds found; unit kept whole, never dropped): "
            f"{', '.join(qa['placed_trim_fallback'][:8])}")
    if qa.get("placed_vision_trimmed"):
        log(f"    placed mode: {len(qa['placed_vision_trimmed'])} unit(s) placed on a Gemini "
            f"vision trim box (clamped against the algorithmic ink bounds): "
            f"{', '.join(qa['placed_vision_trimmed'][:8])}")
    if qa.get("placed_vision_fallback"):
        log(f"    placed mode: {len(qa['placed_vision_fallback'])} unit(s) whose vision trim "
            f"entry fell back to the algorithmic trim: "
            f"{', '.join(qa['placed_vision_fallback'][:8])}")
    if plan.get("chapter_cards"):
        from .editor import chapter_cards_to_timestamps as _ts
        log("    chapter cards: " + "; ".join(_ts(plan["chapter_cards"])))
    _ncall = sum(len(sh.get("callouts") or []) for sh in plan.get("shots") or [])
    if _ncall:
        log(f"    {_ncall} keyword callout(s) (impact/reveal peaks only)")
    if plan.get("sfx"):
        log(f"    {len(plan['sfx'])} SFX event(s) (missing files render silent, never fail)")
    if plan.get("captions_enabled"):
        log(f"    {len(plan.get('captions') or [])} caption chunk(s) (auto-captions on; key word yellow)")
    _bs = plan.get("beat_sync") or {}
    if _bs.get("enabled"):
        log(f"    beat sync: {_bs.get('moved')} cut(s) snapped to beats (max shift {_bs.get('max_shift')}s)")
    if (plan.get("hook") or {}).get("warning"):
        log(f"    ! hook: {plan['hook']['warning']}")
    if qa["review_queue"]:
        log(f"    ! {len(qa['review_queue'])} sentence(s) flagged for review (non-blocking; "
            f"see video/chapter_{chapter}.qa.json)")
    # P2: no silent change — every auto-switch and every timing warning is
    # logged with its fix, and both ride in the QA file for the reviewer.
    if qa.get("layout_switches"):
        log("    layout auto-switches (pair-safe shape): " + "; ".join(
            f"{sw.get('panel')} {_shape_label(sw.get('from_shape'))}->{_shape_label(sw.get('to_shape'))}"
            for sw in qa["layout_switches"][:12]))
    for w in qa.get("timing_warnings") or []:
        log(f"    ! timing: {w.get('code')}: {w.get('message')} Fix: {w.get('fix')}")
    if storyboard:
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
    # P2: the estimated-timing refusal names the exact whisper failure and
    # its fix (from the P0 timing warnings), not just "whisper couldn't".
    timing_warnings = list(plan.get("timing_warnings") or [])
    if not timing_warnings:
        try:
            _align_data = json.loads(align_engine.alignment_path(root, chapter).read_text(encoding="utf-8"))
            timing_warnings = list(_align_data.get("timing_warnings") or [])
        except (OSError, ValueError):
            timing_warnings = []
    timing_detail = "".join(
        f"\n  - {w.get('code')}: {w.get('message')} Fix: {w.get('fix')}" for w in timing_warnings)
    if plan.get("sync") == "estimated" and not allow_estimated:
        raise UserError(
            f"Chapter {chapter}'s timing was only estimated from text length, not synced to the real "
            f"narration (whisper couldn't transcribe it) — the pictures will drift out of sync with the voice.\n"
            f"Fix the sync first: lower `whisper_model` in manhwa-ocr.yaml (e.g. to base.en or tiny.en if your "
            f"machine is low on RAM), then rebuild the plan:  video plan --chapter {chapter} --force\n"
            f"Why the timing is estimated:{timing_detail or '  (no detail recorded — re-run video align to capture it)'}\n"
            f"To render with the estimated timing anyway, pass --force to render.")
    if plan.get("sync") == "synced":
        # The sync gate: the audit samples the plan against the narration and
        # fails on wrong-picture time and late cuts. It runs here, on the plan
        # about to be rendered, and its report stays beside it for status.
        # Reuse a fresh audit instead of recomputing it: the agent workflow
        # already runs `video sync-audit` before render, and an audit is only
        # valid for the plan it was computed from (mtime check).
        report = None
        audit_file = syncaudit.audit_path(root, chapter)
        plan_file = plan_path(root, chapter)
        if (audit_file.exists() and plan_file.exists()
                and audit_file.stat().st_mtime >= plan_file.stat().st_mtime):
            try:
                cached = json.loads(audit_file.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                cached = None
            if (isinstance(cached, dict) and isinstance(cached.get("gate"), dict)
                    and isinstance(cached.get("wrong_picture"), dict)
                    and "ratio" in cached["wrong_picture"]):
                # P2: a cached audit is only reused when it was computed
                # under the CURRENT sync settings, and its gate is
                # re-evaluated from its own numbers through the same code
                # path as audit() (syncaudit.gate_from_report) — a cached
                # PASS under looser settings never sneaks past a stricter
                # config, and the two gate paths cannot drift apart.
                _st = sync_mod.effective_settings(cfg.video, log=lambda _m: None)
                if cached.get("settings") != _st:
                    report = None
                else:
                    cached["gate"] = syncaudit.gate_from_report(cached, _st)
                    report = cached
                if report is not None:
                    log("  sync gate: using the existing audit (plan unchanged since it ran)")
        if report is None:
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
          preview: bool = False, workers: int = 0, log: Callable = print,
          storyboard: bool = True) -> Dict[str, str]:
    """Plan + storyboard + render for each chapter, skipping what's already current."""
    results: Dict[str, str] = {}
    for chapter in sort_chapters(chapters):
        log(f"=== Chapter {chapter} ===")
        if audio_path(sess.path, chapter) is None:
            results[chapter] = "skipped: no audio"
            log(f"  no audio yet (save it as audio/chapter_{chapter}.mp3)")
            continue
        try:
            if cues_mod.load_cues_strict(sess.path, chapter):
                _align_result = align_engine.build_alignment(sess.path, chapter, cfg, sync=sync, log=log)
                # P2: timing warnings are action, not trivia — log each with
                # its fix here (align), in the plan/QA (make_plan), and in
                # the render refusal, so estimated timing is never silent.
                for _w in (_align_result or {}).get("timing_warnings") or []:
                    log(f"  ! timing: {_w.get('code')}: {_w.get('message')} Fix: {_w.get('fix')}")
            make_plan(sess, chapter, cfg, force=force, sync=sync, fmt=fmt, log=log, storyboard=storyboard)
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


def _clip_plan(plan: Dict[str, Any], start: float, end: float, fmt: str) -> Dict[str, Any]:
    """Slice a finished plan to [start, end) and re-frame it as `fmt`.

    Pure plan surgery (no I/O): shots are cut to the window, card entrances
    that already happened become plain cuts, callouts / chapter cards / SFX
    shift into clip time, events outside drop out. The narration audio is
    sliced by the renderer via `audio_offset` — the recording is never
    re-recorded or regenerated.
    """
    duration = float(plan["duration"])
    if not 0 <= start < end <= duration + 0.01:
        raise UserError(f"Clip range {start}-{end}s must be inside the chapter (0-{duration:.1f}s).")
    if fmt in {"9:16", "shorts"} and end - start > 180:
        pass  # the caller logs the 3-minute Shorts warning; this stays pure
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
            card = {**c, "at": round(max(0.0, at), 3), "enter": c.get("enter") if at >= 0 else "cut"}
            if cut_in > 0 and isinstance(c.get("panelfit"), dict):
                # Panel-fit span slices are phased from the shot's own start:
                # skipping the head must advance the slice to the span position
                # the clip actually opens on, or the motion restarts and jumps
                # at the next seam (B1d F1).
                card["panelfit"] = panelfit_advance_slice(c["panelfit"], cut_in)
            cards.append(card)
        # P3: callouts ride on shots with absolute times — shift them into
        # the clip timeline and drop ones outside the clip.
        clip_callouts = [{"t": round(float(c["t"]) - start, 3), "text": c.get("text"),
                          "style": c.get("style"), "dur": c.get("dur", 1.2)}
                         for c in (s.get("callouts") or [])
                         if isinstance(c, dict) and a <= float(c.get("t") or -1) <= b]
        extra: Dict[str, Any] = {"callouts": clip_callouts}
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
    # P3: chapter cards + SFX shift with the clip; events outside it drop out.
    clip_cards = [{**c, "t": round(float(c["t"]) - start, 3)}
                  for c in (plan.get("chapter_cards") or [])
                  if isinstance(c, dict) and start <= float(c.get("t") or -1) < end]
    clip_sfx = [{**e, "t": round(float(e["t"]) - start, 3)}
                for e in (plan.get("sfx") or [])
                if isinstance(e, dict) and start <= float(e.get("t") or -1) < end]
    clip = {**plan, "format": fmt, "shots": shots, "duration": round(end - start, 3), "audio_offset": start,
            "chapter_cards": clip_cards, "sfx": clip_sfx}
    return clip


def make_clip(sess, chapter: str, cfg, start: float, end: float, *, fmt: str | None = None,
              preview: bool = False, workers: int = 0, log: Callable = print) -> Path:
    """Cut a highlight from a chapter's plan, optionally re-framed as 9:16."""
    root, chapter = sess.path, require_chapter(chapter)
    panels = load_panels(root, chapter)
    if not panels:
        raise UserError(f"Plan the chapter first:  video plan --chapter {chapter}")
    plan = load_plan(root, chapter, {p["id"] for p in panels["panels"]})
    fmt = fmt or plan.get("format", "16:9")
    if fmt in {"9:16", "shorts"} and end - start > 180:
        log("  ! YouTube Shorts are limited to 3 minutes; this clip is longer.")
    clip = _clip_plan(plan, start, end, fmt)
    tag = f"clip_{start:g}-{end:g}".replace(".", "_") + ("_9x16" if fmt in {"9:16", "shorts"} else "")
    return render(root, chapter, clip, cfg.video, preview=preview, workers=workers, log=log, name=tag)


# ------------------------------------------------------------------ P5: Shorts + end screen

def shorts_manifest_path(root: Path, chapter: str) -> Path:
    return video_dir(root) / f"chapter_{require_chapter(chapter)}.shorts.json"


def endscreen_path(root: Path, chapter: str) -> Path:
    return video_dir(root) / f"chapter_{require_chapter(chapter)}.endscreen.json"


def watch_next_path(root: Path, chapter: str) -> Path:
    return video_dir(root) / f"chapter_{require_chapter(chapter)}.watch_next.txt"


def make_end_screen(sess, chapter: str, cfg=None, *, log: Callable = print) -> Dict[str, Any]:
    """Write `video/chapter_N.endscreen.json` + `video/chapter_N.watch_next.txt`
    from the finished plan. The plan's `end_screen` is the window/slots spec
    (P5); this resolves the series name for the playlist slot and the WATCH
    NEXT block. Never burns cards into pixels — YouTube Studio adds them."""
    from .shorts import build_end_screen

    root, chapter = sess.path, require_chapter(chapter)
    panels = load_panels(root, chapter)
    if not panels:
        raise UserError(f"Plan the chapter first:  video plan --chapter {chapter}")
    plan = load_plan(root, chapter, {p["id"] for p in panels["panels"]})
    spec = build_end_screen(float(plan["duration"]), chapter, series_name=getattr(sess, "name", None) or sess.slug)
    atomic_write_text(endscreen_path(root, chapter), json.dumps(spec, indent=1, ensure_ascii=False))
    atomic_write_text(watch_next_path(root, chapter), spec["watch_next"] + "\n")
    log(f"  wrote video/chapter_{chapter}.endscreen.json + video/chapter_{chapter}.watch_next.txt")
    return spec


def make_shorts(sess, chapter: str, cfg, *, count: int = 3, preview: bool = False, workers: int = 0,
                log: Callable = print) -> Dict[str, Any]:
    """Build up to `count` vertical 9:16 Shorts from a finished chapter plan.

    Picks the highest-impact moments (hook + payoffs), slices the chapter's
    own narration audio to each window (never re-recorded / generated),
    re-frames panels vertically (face-centred crops, no black bars — the
    plan's crop rules run in 9:16), keeps keyword callouts, and ends each
    Short on a "Full story on channel" card. Writes the manifest
    `video/chapter_N.shorts.json` plus `video/chapter_N.shorts_1.mp4` etc.
    """
    from .shorts import END_CARD_DUR, END_CARD_TEXT, pick_shorts_windows, shorts_manifest, validate_shorts_manifest

    root, chapter = sess.path, require_chapter(chapter)
    panels = load_panels(root, chapter)
    if not panels:
        raise UserError(f"Plan the chapter first:  video plan --chapter {chapter}")
    plan = load_plan(root, chapter, {p["id"] for p in panels["panels"]})
    windows = pick_shorts_windows(plan, count=count)
    if not windows:
        raise UserError(f"Chapter {chapter} has no shots to cut Shorts from.")
    manifest = shorts_manifest(plan, windows)
    problems = validate_shorts_manifest(manifest)
    if problems:
        raise UserError("Internal error: generated Shorts manifest is invalid:\n  - " + "\n  - ".join(problems))
    outputs: List[str] = []
    for entry in manifest["shorts"]:
        start, end = float(entry["start"]), float(entry["end"])
        clip = _clip_plan(plan, start, end, "9:16")
        # End card: a chapter-card-style overlay on the Short's last shot,
        # rendered by the existing chapter-card machinery (frames.py) —
        # text only, no new audio, no burned YouTube end-screen cards.
        clip["chapter_cards"] = [{**c} for c in clip.get("chapter_cards") or []] + [
            {"t": round(max(0.0, clip["duration"] - END_CARD_DUR), 3),
             "title": END_CARD_TEXT, "dur": END_CARD_DUR}]
        out = render(root, chapter, clip, cfg.video, preview=preview, workers=workers, log=log,
                     name=f"shorts_{entry['index']}")
        outputs.append(out.name)
        log(f"  short {entry['index']}: {start:.1f}s-{end:.1f}s -> {out.name} ({entry['title_suggestion']})")
    manifest["files"] = outputs
    atomic_write_text(shorts_manifest_path(root, chapter), json.dumps(manifest, indent=1, ensure_ascii=False))
    log(f"  wrote video/chapter_{chapter}.shorts.json ({len(outputs)} Shorts)")
    return manifest


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
