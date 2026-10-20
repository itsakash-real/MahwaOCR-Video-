"""Render a plan to MP4.

Each shot becomes one segment, drawn frame by frame (frames.py) and piped into
FFmpeg. Segments are cached under .work/video/chN/ by a hash of everything that
affects their pixels, including the previous shot (its tail is drawn under this
shot's crossfade). Editing one shot in the plan therefore re-renders that shot
and the one after it, nothing else.

Segments share identical encoder settings, so joining them is a stream copy;
the narration (and optional ducked music) is muxed in the same pass. Joins
are hard cuts by design (frame-exact bounds from plan.shot_frame_bounds),
never crossfades. When the plan has captions enabled, a final ffmpeg pass
burns video/chapter_N.captions.ass in with the subtitles filter -- captions
never touch the per-shot segment cache, and a subtitles-filter failure
keeps the un-captioned video instead of failing the render.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from collections import OrderedDict
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Callable, Dict, List

from ..util import UserError, require_chapter
from ..workspace import work_root
from .ffmpeg import close_encoder, open_encoder, run
import numpy as np

from .editor import SFX_VOLUME, resolve_sfx_files
from .frames import Format, ShotRenderer, format_from
from .media import pages_dir, video_dir
from .panels import load_panels, panel_image

RENDER_VERSION = 2
AUDIO_FADE = 0.8


def output_path(root: Path, chapter: str, preview: bool = False, name: str | None = None) -> Path:
    base = f"chapter_{require_chapter(chapter)}" + (f".{name}" if name else "")
    return video_dir(root) / f"{base}{'.preview' if preview else ''}.mp4"


def _cache_dir(root: Path, chapter: str, preview: bool, name: str | None = None) -> Path:
    # Clips get their own folder: pruning a clip's cache must never throw away
    # the chapter's segments (or the other way round).
    kind = "clip-segments" if name else "segments"
    return work_root(root) / "video" / f"ch{require_chapter(chapter)}" / (f"{kind}-preview" if preview else kind)


# ------------------------------------------------------------------ worker (runs in a child process)

_PANEL_CACHE: "OrderedDict[tuple, Any]" = OrderedDict()
_PANEL_CACHE_MAX = 32


def _panel(root: str, chapter: str, panel: Dict[str, Any], norm_w: int, fingerprint: str):
    # An intro panel is read straight from the chapter it came from (src_chapter / src_norm_w).
    chapter = panel.get("src_chapter") or chapter
    norm_w = panel.get("src_norm_w") or norm_w
    key = (root, chapter, panel["id"], fingerprint, panel.get("src_fingerprint"))
    img = _PANEL_CACHE.get(key)
    if img is None:
        img = panel_image(Path(root), chapter, panel, norm_w, clean=True, fingerprint=fingerprint)
        _PANEL_CACHE[key] = img
        # LRU: evict one entry, never wipe the whole cache (the old
        # `clear()` made workers re-decode panels they had just loaded).
        while len(_PANEL_CACHE) > _PANEL_CACHE_MAX:
            _PANEL_CACHE.popitem(last=False)
    else:
        _PANEL_CACHE.move_to_end(key)
    return img


def render_segment(job: Dict[str, Any]) -> str:
    """Draw and encode one shot. Top-level so worker processes can import it."""
    fmt = Format(*job["fmt"])
    images = {pid: _panel(job["root"], job["chapter"], p, job["norm_w"], job["fingerprint"])
              for pid, p in job["panels"].items()}
    if job["shot"].get("cinematic"):
        # Cinematic mode: full-screen panels, no cards (video/cinematic.py).
        from .cinematic import CinematicShot
        shot = CinematicShot(job["shot"], images, fmt,
                             zoom=float((job.get("cinematic_cfg") or {}).get("zoom", 0.06)))
    else:
        shot = ShotRenderer(job["shot"], images, fmt, job["effects_on"],
                            video_end=job["video_end"], end_fade=job["end_fade"],
                            chapter_cards=job.get("chapter_cards") or [])

    out = Path(job["out"])
    tmp = out.with_name(out.stem + ".part.mp4")
    enc = open_encoder(tmp, fmt.width, fmt.height, fmt.fps, crf=job["crf"], preset=job["preset"])
    try:
        for f in range(job["f0"], job["f1"]):
            # Once a shot settles, frame_array returns the same cached frame:
            # A hand-set hold shot returns a cached frame once settled;
            # continuous-motion shots are re-drawn every frame (defect 1).
            arr = shot.frame_array(f / fmt.fps)
            enc.stdin.write(memoryview(np.ascontiguousarray(arr)))
    except BrokenPipeError:
        pass  # close_encoder reports ffmpeg's actual error
    finally:
        close_encoder(enc)
    os.replace(tmp, out)
    return str(out)


# ------------------------------------------------------------------ orchestration


def _jobs(root: Path, chapter: str, plan: Dict[str, Any], panels_data: Dict[str, Any], fmt: Format,
          vcfg: Dict[str, Any], preview: bool, name: str | None = None) -> List[Dict[str, Any]]:
    panels = {p["id"]: p for p in panels_data["panels"]}
    shots = plan["shots"]
    duration = float(plan["duration"])
    from .plan import shot_frame_bounds
    bounds = shot_frame_bounds(shots, fmt.fps, duration)
    effects_on = {k: bool(v) for k, v in (plan.get("effects_enabled") or vcfg.get("effects") or {}).items()}
    end_fade = effects_on.get("end_fade", True)
    crf = int(vcfg.get("crf", 20)) + (6 if preview else 0)
    preset = "ultrafast" if preview else str(vcfg.get("preset", "veryfast"))
    cache = _cache_dir(root, chapter, preview, name)

    jobs = []
    for i, shot in enumerate(shots):
        (f0, f1) = bounds[i]
        if f1 <= f0:
            raise UserError(f"Shot {shot.get('id')} is shorter than one frame. Lengthen it in the plan.")
        last = i == len(shots) - 1
        used = [c["panel"] for c in shot.get("cards") or []] + ([shot["bg_panel"]] if shot.get("bg_panel") else [])
        # Per-page stamps for exactly the page files this shot's panels come
        # from. The old key used the chapter-wide panels fingerprint, so a
        # no-op re-detection (one panel's geometry re-measured identically)
        # invalidated every cached segment. Geometry is already in the key
        # via the panels dict; the stamps catch a page file being replaced.
        page_stamp: Dict[str, Any] = {}
        for pid in dict.fromkeys(used):
            p = panels.get(pid)
            if p is None:
                continue
            ch = p.get("src_chapter") or chapter
            for seg in p.get("segments") or []:
                f = pages_dir(root, ch) / seg["file"]
                try:
                    st = f.stat()
                    page_stamp[f"{ch}/{seg['file']}"] = (st.st_size, st.st_mtime)
                except OSError:
                    page_stamp[f"{ch}/{seg['file']}"] = None
        job = {
            "root": str(root), "chapter": chapter, "norm_w": panels_data["norm_width"],
            "fingerprint": panels_data.get("fingerprint", ""),
            "panels": {pid: panels[pid] for pid in dict.fromkeys(used) if pid in panels},
            "shot": shot, "fmt": (fmt.width, fmt.height, fmt.fps), "f0": f0, "f1": f1,
            "chapter_cards": list(plan.get("chapter_cards") or []),
            "video_end": duration if last else None, "end_fade": end_fade and last,
            "effects_on": effects_on, "crf": crf, "preset": preset, "page_stamp": page_stamp,
        }
        # The chapter fingerprint is deliberately NOT in the key (it feeds
        # _panel's cleaned-panel cache instead): it changes on any
        # re-detection, even one that leaves every panel identical.
        key = hashlib.sha256(json.dumps([RENDER_VERSION, {k: v for k, v in job.items()
                                                           if k not in ("root", "fingerprint")}],
                                        sort_keys=True, default=str).encode()).hexdigest()[:20]
        job["out"] = str(cache / f"{key}.mp4")
        jobs.append(job)
    return jobs


def _audio_graph(has_music: bool, duration: float, volume: float,
                   sfx_inputs: List[tuple] | None = None) -> str:
    """Narration + ducked music + timed SFX.

    sfx_inputs: [(input_index, delay_ms, type)] — one ffmpeg input per SFX
    event (inputs are added by render()). Each SFX is delayed to its event
    time, kept below the narration, and mixed in. Empty list == old graph.
    Music fades in (>=0.3s) and the final mix fades out; the sidechain
    compressor ducks the bed under speech and lets it rise (toward the
    60-80% solo target) when the narration pauses.
    """
    fade = f"afade=t=out:st={max(0.0, duration - AUDIO_FADE):.3f}:d={AUDIO_FADE}"
    parts: List[str] = []
    # Narration is always input 1; music is input 2 when present; SFX inputs
    # follow (their indices are assigned by render()).
    if has_music:
        parts.append(
            f"[1:a]aresample=48000,asplit=2[narr][key];"
            f"[2:a]aresample=48000,atrim=0:{duration:.3f},"
            f"afade=t=in:st=0:d=0.5,volume={volume:.3f}[mus];"
            f"[mus][key]sidechaincompress=threshold=0.02:ratio=12:attack=15:release=450[duck]")
        mix_inputs = ["[narr]", "[duck]"]
    else:
        parts.append("[1:a]aresample=48000[narr]")
        mix_inputs = ["[narr]"]
    for i, (idx, delay_ms, _kind) in enumerate(sfx_inputs or []):
        parts.append(f"[{idx}:a]aresample=48000,adelay={int(delay_ms)}|{int(delay_ms)},"
                     f"volume={SFX_VOLUME:.3f}[sfx{i}]")
        mix_inputs.append(f"[sfx{i}]")
    n = len(mix_inputs)
    parts.append(f"{''.join(mix_inputs)}amix=inputs={n}:duration=first:normalize=0,{fade}[aout]")
    return ";".join(parts)


def render(root: Path, chapter: str, plan: Dict[str, Any], vcfg: Dict[str, Any], *, preview: bool = False,
           workers: int = 0, log: Callable[[str], None] = print, name: str | None = None,
           panels_data: Dict[str, Any] | None = None) -> Path:
    chapter = require_chapter(chapter)
    panels_data = panels_data or load_panels(root, chapter)
    if not panels_data:
        raise UserError(f"No panels for chapter {chapter}. Run:  video panels --chapter {chapter}")
    fmt = format_from(plan.get("format", "16:9"), int(plan.get("fps", 30)), preview)
    jobs = _jobs(root, chapter, plan, panels_data, fmt, vcfg, preview, name)

    cache = _cache_dir(root, chapter, preview, name)
    cache.mkdir(parents=True, exist_ok=True)
    todo = [j for j in jobs if not Path(j["out"]).exists()]
    log(f"  {len(jobs)} shots, {len(jobs) - len(todo)} cached, rendering {len(todo)} at "
        f"{fmt.width}x{fmt.height}{' (preview)' if preview else ''}")

    started = time.time()
    if todo:
        n = workers or int(vcfg.get("workers") or 0) or max(1, os.cpu_count() or 2)
        n = max(1, min(n, len(todo)))
        if n == 1:
            for k, job in enumerate(todo, start=1):
                render_segment(job)
                log(f"    {k}/{len(todo)} shots")
        else:
            with ProcessPoolExecutor(max_workers=n) as pool:
                futures = [pool.submit(render_segment, j) for j in todo]
                for k, fut in enumerate(as_completed(futures), start=1):
                    fut.result()
                    if k % max(1, len(todo) // 10) == 0 or k == len(todo):
                        log(f"    {k}/{len(todo)} shots ({time.time() - started:.0f}s)")

    # Join: relative names in the list, so non-Latin series folders can't trip it up.
    listing = cache / "concat.txt"
    listing.write_text("".join(f"file '{Path(j['out']).name}'\n" for j in jobs), encoding="utf-8")

    out = output_path(root, chapter, preview, name)
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_name(out.stem + ".part.mp4")
    duration = float(plan["duration"])
    args = ["-f", "concat", "-safe", "0", "-i", str(listing)]

    audio = plan.get("audio")
    if audio:
        audio_file = root / audio
        if not audio_file.is_file():
            raise UserError(f"The plan's audio file is missing: {audio_file}")
        offset = float(plan.get("audio_offset") or 0)
        if offset > 0:
            args += ["-ss", f"{offset:.3f}"]  # clips start partway through the narration
        args += ["-i", str(audio_file)]
        music = plan.get("music")
        music_file = (root / music) if music else None
        if music_file is not None and not music_file.is_file():
            log(f"  ! music file not found ({music_file}); rendering without music")
            music_file = None
        if music_file is not None:
            args += ["-stream_loop", "-1", "-i", str(music_file)]
        # P3 SFX: one ffmpeg input per event with a real file. Missing
        # sfx_dir / files are a warning, never a failure (editor.py).
        sfx_inputs: List[tuple] = []
        _sfx_dir = (vcfg or {}).get("sfx_dir") if isinstance(vcfg, dict) else None
        _found, _missing = resolve_sfx_files(root, _sfx_dir)
        if _missing and (plan.get("sfx") or []):
            log(f"  ! SFX missing for {', '.join(sorted(set(_missing)))} "
                f"(no file in {_sfx_dir or 'sfx_dir'}); rendering without those SFX")
        _next_idx = 3 if music_file is not None else 2
        for _ev in plan.get("sfx") or []:
            _f = _found.get(_ev.get("type"))
            if _f is None:
                continue
            args += ["-i", str(_f)]
            sfx_inputs.append((_next_idx, round(float(_ev.get("t") or 0) * 1000), _ev.get("type")))
            _next_idx += 1
        # 48 kHz stereo everywhere, so chapter videos (and title cards) can be
        # joined by stream copy regardless of how each narration was recorded.
        args += ["-filter_complex", _audio_graph(music_file is not None, duration,
                                                 float(plan.get("music_volume", 0.22)),
                                                 sfx_inputs),
                 "-map", "0:v", "-map", "[aout]", "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-ac", "2"]
    else:
        args += ["-map", "0:v"]
    args += ["-c:v", "copy", "-t", f"{duration:.3f}", "-movflags", "+faststart", str(tmp)]
    run(args, timeout=1800)
    assembled = tmp
    # CapCut auto-captions: burn the plan's .ass in with the subtitles
    # filter. Captions are a final re-encode pass, not part of the shot
    # segments, so editing captions never re-renders a shot. Old plans
    # (no captions key) skip this; `video captions` adds captions to one.
    _caps = plan.get("captions") or []
    if plan.get("captions_enabled") and _caps:
        from .captions import captions_path, write_ass
        _ass = captions_path(root, chapter)
        write_ass(_caps, _ass, fmt=fmt)
        _escaped = str(_ass).replace("\\", "/").replace(":", "\\:").replace("'", "\\'")
        _captioned = tmp.with_name(tmp.stem + ".captioned.mp4")
        _preset = "ultrafast" if preview else str(vcfg.get("preset", "veryfast"))
        _crf = int(vcfg.get("crf", 20)) + (6 if preview else 0)
        try:
            run(["-i", str(tmp), "-vf", f"subtitles={_escaped}", "-map", "0",
                 "-c:v", "libx264", "-preset", _preset, "-crf", str(_crf),
                 "-c:a", "copy", "-movflags", "+faststart", str(_captioned)], timeout=1800)
            assembled = _captioned
            log(f"  captions burned in ({len(_caps)} chunks)")
        except UserError:
            log("  ! captions could not be burned (subtitles filter failed); keeping the video without captions")
    os.replace(assembled, out)

    # Drop segments no longer referenced by this plan, so the cache can't grow forever.
    keep = {Path(j["out"]).name for j in jobs}
    for f in cache.glob("*.mp4"):
        if f.name not in keep:
            f.unlink(missing_ok=True)

    log(f"  wrote {out.relative_to(root).as_posix()} in {time.time() - started:.0f}s")
    return out
