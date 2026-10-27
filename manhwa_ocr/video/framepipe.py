"""Genius Knight movie mode — the frame-pipe renderer (long-form movie builds).

This is the consolidation of the six chapter-specific Phase B renderers
that produced the delivered Genius Knight 6-chapter movie
(``work_movie/ch*_phaseB_work/frame_pipe_render_*.py`` and
``hook_intro_work2/frame_pipe_render.py`` in the Genius Knight session).
Those scripts were near-duplicates with absolute paths baked in; this
module is the single generic version, with no chapter, path, or script
content copied from any chapter.

Style, pinned (user locks, 2026-10-26):
  * 1920x1080, 30 fps, full-bleed 16:9 centre crop. No pillars, no blur
    backdrop, no floating, no scrolling/traversing, zero white space
    by construction (every output frame is a full 1920x1080 crop).
  * Motion is the Solo rotation, very slow, in shot/run order:
    zoom_in -> slide_up -> slide_down -> zoom_out -> zoom_in ...
    Zooms span 1.00 <-> 1.05 centred; slides travel exactly 115 output
    pixels over the shot. Every shot's frame 0 is its full settled
    composition: no fade, no slide-in, no settle ramp, hard cuts only.
  * One shot per sentence (sentence-synced spans, never the intro's
    fixed 2-seconds-per-image rhythm). Consecutive shots that reuse the
    same source + window form one run: the motion runs continuously
    over the run, so the repeat is an invisible hold, not a jump cut.
  * Approved cuts only. A shot's source must be an existing cut image
    exactly 900 px wide, cropped vertically only (full width, ``y0`` to
    ``y0 + win_h``). Anything else is a hard fail — this module never
    silently substitutes a pipeline / re-detected panel for an approved
    cut. If approved cuts cannot be used, the build stops.
  * No captions, no title cards, no endscreen, no intro/outro, no fades
    in movie mode. The combined movie is a stream-copy concat of the
    chapter videos: no intro, no chapter breaks, no re-encode.
  * Movie-mode voice (config, see ``video.movie`` in ``config.py``):
    Deepgram aura-2-odysseus-en @ 1.3. This module does not synthesise
    audio; it muxes a caller-supplied narration track, uncut, with
    ``-map 0:v -map 1:a`` and no ``-shortest``.

This module deliberately does NOT touch the Shorts path
(``video/shorts.py``) or the Solo placed path (``video.mode == "placed"``):
those modes keep their own renderers and defaults intact.
"""
from __future__ import annotations

import json
import math
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Sequence

from PIL import Image

WIDTH = 1920
HEIGHT = 1080
FPS = 30
CUT_WIDTH = 900
ZOOM_SPAN = 0.05
SLIDE_PX = 115
ROTATION = ("zoom_in", "slide_up", "slide_down", "zoom_out")
MOVIE_VOICE = "aura-2-odysseus-en"
MOVIE_VOICE_SPEED = 1.3
NEAR_WHITE_THRESHOLD = 0.30  # share of pixels >= 245 on all channels


@dataclass
class Shot:
    """One sentence-synced shot.

    ``src`` is an approved cut image (width exactly 900). ``y0``/``win_h``
    select the subject window vertically only; the crop is always the
    full 900 px width. ``start``/``end`` are seconds in the narration
    track; ``frames`` may be pre-computed, otherwise it is allocated
    from the span by :func:`allocate_frames`. ``sentence`` is the
    narration sentence id this shot belongs to (for coverage checks);
    ``panel`` is the cut id, for reports only.
    """

    src: str | Path
    y0: int
    win_h: int
    start: float
    end: float
    sentence: int | str | None = None
    panel: str | None = None
    motion: str | None = None
    frames: int | None = None
    # Run grouping (consecutive shots sharing src+window): filled by
    # assign_motions(). run_frames/run_offset describe the whole run.
    run_frames: int | None = None
    run_offset: int | None = None

    def window_key(self) -> tuple:
        return (str(self.src), int(self.y0), int(self.win_h))


def validate_shots(shots: Sequence[Shot], cuts_dir: str | Path | None = None) -> None:
    """Approved-cuts-only gate. Raises ValueError on the first violation.

    Never substitutes anything: a missing file, a non-900 width, a
    window outside the image, or a source outside ``cuts_dir`` (when
    given) is a hard fail, not a fallback to detection.
    """
    allowed = Path(cuts_dir).resolve() if cuts_dir is not None else None
    for shot in shots:
        src = Path(shot.src)
        if not src.is_file():
            raise ValueError(f"approved cut missing, refusing to substitute: {src}")
        if allowed is not None:
            try:
                src.resolve().relative_to(allowed)
            except ValueError:
                raise ValueError(
                    f"shot source outside approved cuts dir {allowed}: {src}"
                ) from None
        with Image.open(src) as im:
            width, height = im.size
        if width != CUT_WIDTH:
            raise ValueError(
                f"approved cut must be {CUT_WIDTH}px wide, got {width}: {src}"
            )
        if shot.win_h <= 0 or shot.y0 < 0 or shot.y0 + shot.win_h > height:
            raise ValueError(
                f"window y0={shot.y0} win_h={shot.win_h} outside image "
                f"height {height}: {src}"
            )
        if shot.end <= shot.start:
            raise ValueError(f"shot span must be positive: {shot.start}..{shot.end}")


def check_sentence_coverage(sentences: Iterable, shots: Sequence[Shot]) -> None:
    """Every sentence used exactly once; every mapped shot used.

    ``sentences`` is the narration's sentence ids in order. A shot with
    ``sentence=None`` cannot be coverage-checked and is rejected: in
    movie mode there are no unmapped shots.
    """
    expected = list(sentences)
    got = []
    for shot in shots:
        if shot.sentence is None:
            raise ValueError("movie-mode shot without a sentence id cannot be checked")
        got.append(shot.sentence)
    if sorted(map(str, got)) != sorted(map(str, expected)):
        missing = [s for s in expected if s not in got]
        extra = [s for s in got if s not in expected]
        dupes = sorted({s for s in got if got.count(s) > 1}, key=str)
        raise ValueError(
            f"sentence coverage failed: missing={missing} extra={extra} duplicated={dupes}"
        )


def allocate_frames(shots: Sequence[Shot], total_seconds: float, fps: int = FPS) -> int:
    """Largest-remainder allocation so total frames == round(total*fps).

    This is the ch2 Phase B allocation: minimal per-shot drift, exact
    total, so video duration matches the narration manifest exactly.
    Shots with ``frames`` already set keep them and are excluded from
    allocation; the remaining target is allocated over the rest.
    """
    target = int(round(total_seconds * fps))
    preset = sum(s.frames for s in shots if s.frames is not None)
    open_shots = [s for s in shots if s.frames is None]
    if open_shots:
        raw = [(s.end - s.start) * fps for s in open_shots]
        base = [int(x) for x in raw]
        rem = (target - preset) - sum(base)
        if rem < 0:
            raise ValueError("preset shot frames already exceed total frames")
        order = sorted(range(len(open_shots)), key=lambda i: (-(raw[i] - base[i]), i))
        for k in range(rem):
            base[order[k % len(order)]] += 1
        for shot, n in zip(open_shots, base):
            shot.frames = n
    total = sum(s.frames for s in shots)
    if total != target:
        raise ValueError(f"frame allocation {total} != target {target}")
    return total


def assign_motions(shots: Sequence[Shot]) -> None:
    """Assign the Solo rotation, one motion per run.

    Consecutive shots sharing src+window are one run; the rotation
    advances per run, not per shot, and progress is measured over the
    whole run (see :func:`render_movie`). A shot's explicit ``motion``
    is honoured and does not advance the rotation.
    """
    run_index = 0
    i = 0
    while i < len(shots):
        j = i
        while j + 1 < len(shots) and shots[j + 1].window_key() == shots[i].window_key():
            j += 1
        run = shots[i : j + 1]
        explicit = run[0].motion
        motion = explicit or ROTATION[run_index % len(ROTATION)]
        run_frames = sum(s.frames or 0 for s in run)
        offset = 0
        for shot in run:
            shot.motion = motion
            shot.run_frames = run_frames
            shot.run_offset = offset
            offset += shot.frames or 0
        if explicit is None:
            run_index += 1
        i = j + 1


def full_bleed_base(cut: Image.Image, y0: int, win_h: int, motion: str) -> Image.Image:
    """The 1920-wide base frame for one shot's window.

    Zooms start from a 1920x1080 base; slides start from a 1920x1195
    base (1080 + 115 slide travel) so no slide ever exposes an edge.
    """
    crop = cut.crop((0, y0, CUT_WIDTH, y0 + win_h))
    if motion.startswith("slide"):
        return crop.resize((WIDTH, HEIGHT + SLIDE_PX), Image.LANCZOS)
    return crop.resize((WIDTH, HEIGHT), Image.LANCZOS)


def frame_at(base: Image.Image, motion: str, progress: float) -> Image.Image:
    """One output frame at ``progress`` in [0, 1] along a motion.

    Frame 0 is always the full settled composition: zoom_in starts at
    1.00, zoom_out at 1.05, slides at a crop edge of the 1195-high base.
    There is no entrance, fade, or slide-in ramp anywhere.
    """
    p = min(1.0, max(0.0, float(progress)))
    if motion == "zoom_in":
        z = 1.0 + ZOOM_SPAN * p
    elif motion == "zoom_out":
        z = 1.0 + ZOOM_SPAN * (1.0 - p)
    elif motion in ("slide_up", "slide_down"):
        y = int(round(SLIDE_PX * p)) if motion == "slide_up" else int(round(SLIDE_PX * (1.0 - p)))
        return base.crop((0, y, WIDTH, y + HEIGHT))
    else:
        raise ValueError(f"unknown movie motion: {motion!r}")
    w, h = int(WIDTH / z), int(HEIGHT / z)
    x, y = (WIDTH - w) // 2, (HEIGHT - h) // 2
    return base.crop((x, y, x + w, y + h)).resize((WIDTH, HEIGHT), Image.LANCZOS)


def _ffmpeg_cmd(out: Path) -> list:
    return [
        "ffmpeg", "-y", "-v", "error",
        "-f", "rawvideo", "-pix_fmt", "rgb24",
        "-s", f"{WIDTH}x{HEIGHT}", "-r", str(FPS), "-i", "-",
        "-i", "",  # audio placeholder, replaced by caller
        "-map", "0:v", "-map", "1:a",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "18",
        "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "192k",
        str(out),
    ]


def render_movie(
    shots: Sequence[Shot],
    audio: str | Path,
    out: str | Path,
    total_seconds: float | None = None,
    cuts_dir: str | Path | None = None,
) -> int:
    """Render ``shots`` + ``audio`` to ``out`` (1920x1080, 30fps).

    Hard cuts only: frames are computed exactly once in PIL and piped
    as rawvideo into ffmpeg; there is no transition, fade, or
    ``-shortest`` (video length is the allocated frame count). Returns
    the number of frames written. Raises ValueError from the
    approved-cuts gate, or RuntimeError if ffmpeg fails.
    """
    validate_shots(shots, cuts_dir=cuts_dir)
    if total_seconds is not None:
        allocate_frames(shots, total_seconds)
    elif any(s.frames is None for s in shots):
        raise ValueError("shots need frames or total_seconds for allocation")
    assign_motions(shots)
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    cmd = _ffmpeg_cmd(out)
    cmd[cmd.index("") ] = str(audio)
    enc = subprocess.Popen(cmd, stdin=subprocess.PIPE)
    total = 0
    try:
        # Base size depends on motion class (slides need the 1195-high
        # base), so the cache key must include it, not just the window.
        bases: dict[tuple, Image.Image] = {}
        for shot in shots:
            key = (shot.window_key(), bool((shot.motion or "").startswith("slide")))
            if key not in bases:
                with Image.open(shot.src) as im:
                    bases[key] = full_bleed_base(
                        im.convert("RGB"), shot.y0, shot.win_h, shot.motion or "zoom_in"
                    )
            base = bases[key]
            run_frames = shot.run_frames or shot.frames or 1
            for fi in range(shot.frames or 0):
                progress = (shot.run_offset or 0) + fi
                p = progress / max(1, run_frames - 1)
                enc.stdin.write(frame_at(base, shot.motion or "zoom_in", p).tobytes())
                total += 1
    finally:
        enc.stdin.close()
    enc.wait()
    if enc.returncode != 0:
        raise RuntimeError(f"ffmpeg failed with rc={enc.returncode} for {out}")
    return total


def gate_first_mid_last(video: str | Path, shots: Sequence[Shot]) -> list[dict]:
    """Re-verification gate: first/mid/last frame of every shot.

    Extracts the three frames per shot with ffmpeg, asserts each is
    1920x1080, and records its near-white share. A near-white share
    above 30% is flagged (source art can be pale; the flag is a review
    item, not a silent pass) — a wrong size or a missing frame is a
    hard failure. Returns one row per shot.
    """
    video = str(video)
    rows: list[dict] = []
    tmp = Path(video).parent / ".framepipe_gate"
    tmp.mkdir(exist_ok=True)
    try:
        for shot in shots:
            span = shot.end - shot.start
            row: dict = {"panel": shot.panel, "sentence": shot.sentence, "motion": shot.motion}
            for tag, t in (
                ("first", shot.start),
                ("mid", shot.start + span / 2),
                ("last", shot.end - 1 / FPS),
            ):
                fpath = tmp / f"{tag}.png"
                # Output seeking (-ss AFTER -i): input seeking to the
                # last frame's start, rounded to 3 decimals, can land
                # 0.3ms past the final frame and return nothing.
                subprocess.run(
                    ["ffmpeg", "-y", "-v", "error",
                     "-i", video, "-ss", f"{t:.6f}", "-frames:v", "1", str(fpath)],
                    check=True,
                )
                with Image.open(fpath) as im:
                    if im.size != (WIDTH, HEIGHT):
                        raise ValueError(
                            f"{tag} frame of shot {shot.panel} is {im.size}, not 1920x1080"
                        )
                    if tag == "mid":
                        import numpy as np

                        arr = np.asarray(im.convert("RGB"))
                        row["near_white"] = float(((arr >= 245).all(axis=2)).mean())
            row["flag_near_white_gt30"] = row["near_white"] > NEAR_WHITE_THRESHOLD
            rows.append(row)
    finally:
        for fpath in tmp.glob("*.png"):
            fpath.unlink(missing_ok=True)
        tmp.rmdir()
    return rows


def concat_movies(inputs: Sequence[str | Path], out: str | Path) -> None:
    """Join chapter videos into ONE continuous movie, stream-copy.

    No intro, no chapter breaks, no title cards, no re-encode: the
    ffmpeg concat demuxer copies the streams as-is.
    """
    inputs = [str(p) for p in inputs]
    if len(inputs) < 2:
        raise ValueError("concat needs at least 2 chapter videos")
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    listfile = out.parent / ".framepipe_concat.txt"
    listfile.write_text("".join(f"file '{p}'\n" for p in inputs))
    try:
        subprocess.run(
            ["ffmpeg", "-y", "-v", "error", "-f", "concat", "-safe", "0",
             "-i", str(listfile), "-c", "copy", str(out)],
            check=True,
        )
    finally:
        listfile.unlink(missing_ok=True)


def shots_from_json(path: str | Path) -> list[Shot]:
    """Load shots from JSON: list of {src, y0, win_h, start, end, ...}."""
    data = json.loads(Path(path).read_text())
    shots = []
    for item in data:
        shots.append(Shot(
            src=item["src"], y0=int(item["y0"]), win_h=int(item["win_h"]),
            start=float(item["start"]), end=float(item["end"]),
            sentence=item.get("sentence"), panel=item.get("panel"),
            motion=item.get("motion"), frames=item.get("frames"),
        ))
    return shots
