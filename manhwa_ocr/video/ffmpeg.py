"""Locating and running FFmpeg.

`ffmpeg` on PATH wins; otherwise the binary bundled by imageio-ffmpeg is used,
so nothing has to be installed by hand on Windows. The bundled build has no
`ffprobe`, so durations are read from `ffmpeg -i` output instead.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from functools import lru_cache
from pathlib import Path
from typing import List, Sequence

from ..util import UserError

INSTALL_HINT = 'pip install -e ".[video]"'


@lru_cache(maxsize=1)
def ffmpeg_exe() -> str:
    found = shutil.which("ffmpeg")
    if found:
        return found
    try:
        import imageio_ffmpeg
    except ImportError:
        raise UserError(f"Video needs FFmpeg. Install it with:  {INSTALL_HINT}") from None
    try:
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception as exc:  # noqa: BLE001 - surfaced with a fix
        raise UserError(f"FFmpeg couldn't be located ({exc}). Reinstall with:  {INSTALL_HINT}") from None


def _tail(text: str, lines: int = 15) -> str:
    return "\n".join((text or "").strip().splitlines()[-lines:])


def run(args: Sequence[str], *, timeout: float | None = None) -> subprocess.CompletedProcess:
    """Run ffmpeg with args. Raises UserError with the useful part of stderr."""
    cmd: List[str] = [ffmpeg_exe(), "-hide_banner", "-loglevel", "error", "-y", *map(str, args)]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                              encoding="utf-8", errors="replace")
    except subprocess.TimeoutExpired:
        raise UserError(f"FFmpeg took longer than {timeout:.0f}s and was stopped.") from None
    if proc.returncode != 0:
        raise UserError(f"FFmpeg failed:\n{_tail(proc.stderr)}")
    return proc


_DURATION = re.compile(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)")


def media_duration(path: Path) -> float:
    """Length of an audio or video file, in seconds."""
    path = Path(path)
    if not path.is_file():
        raise UserError(f"File not found: {path}")
    proc = subprocess.run([ffmpeg_exe(), "-hide_banner", "-i", str(path)],
                          capture_output=True, text=True, encoding="utf-8", errors="replace")
    m = _DURATION.search(proc.stderr or "")
    if not m:
        raise UserError(f"Couldn't read the length of {path.name}. Is it a valid audio/video file?\n{_tail(proc.stderr, 4)}")
    h, mnt, s = m.groups()
    seconds = int(h) * 3600 + int(mnt) * 60 + float(s)
    if seconds <= 0.05:
        raise UserError(f"{path.name} is empty (0 seconds long).")
    return seconds


_TIME = re.compile(r"time=(\d+):(\d+):(\d+(?:\.\d+)?)")
_SILENCE_START = re.compile(r"silence_start:\s*(-?\d+(?:\.\d+)?)")
_SILENCE_END = re.compile(r"silence_end:\s*(-?\d+(?:\.\d+)?)")


def _timing_cache_path(path: Path) -> Path:
    return path.with_name(path.stem + ".audio_timing.json")


def audio_timing(path: Path, *, noise_db: int = -45, min_silence: float = 1.5) -> dict:
    """{"duration": real length, "speech_end": when the last sound stops}.

    Decodes the whole file once (well under a second per minute of audio):
    - the real length, because the header's length is only an estimate for
      some MP3s and can be off by a lot;
    - trailing silence, which TTS exports often pad on. A video should end
      with the voice, not run on over a silent tail.

    The result is cached beside the audio file, keyed by (size, mtime, noise
    settings): plan, align and the sync audit all ask for the same file's
    timing, and there is no reason to decode it three times per cycle.
    """
    path = Path(path)
    cache_path = _timing_cache_path(path)
    st = None
    try:
        st = path.stat()
        cached = json.loads(cache_path.read_text(encoding="utf-8"))
        if (isinstance(cached, dict) and cached.get("size") == st.st_size
                and cached.get("mtime") == int(st.st_mtime)
                and cached.get("noise_db") == noise_db and cached.get("min_silence") == min_silence):
            return {"duration": float(cached["duration"]), "speech_end": float(cached["speech_end"])}
    except (OSError, ValueError):
        pass
    header = media_duration(path)
    proc = subprocess.run([ffmpeg_exe(), "-hide_banner", "-i", str(path), "-vn",
                           "-af", f"silencedetect=noise={noise_db}dB:d={min_silence}", "-f", "null", "-"],
                          capture_output=True, text=True, encoding="utf-8", errors="replace")
    err = proc.stderr or ""
    duration = header
    times = _TIME.findall(err)
    if proc.returncode == 0 and times:
        h, m, s = times[-1]
        decoded = int(h) * 3600 + int(m) * 60 + float(s)
        if decoded > 0.05:
            duration = decoded
    starts = [float(x) for x in _SILENCE_START.findall(err)]
    ends = [float(x) for x in _SILENCE_END.findall(err)]
    speech_end = duration
    # Silence that runs to the end of the file: it never ends, or ends at the end.
    if starts and (len(ends) < len(starts) or ends[-1] >= duration - 0.05):
        speech_end = min(duration, max(0.0, starts[-1]))
    result = {"duration": duration, "speech_end": speech_end}
    if st is not None:
        try:
            cache_path.write_text(json.dumps({**result, "size": st.st_size,
                                              "mtime": int(st.st_mtime),
                                              "noise_db": noise_db, "min_silence": min_silence}),
                                  encoding="utf-8")
        except OSError:
            pass
    return result


def video_info(path: Path) -> dict:
    """Width, height, fps and whether there's an audio stream. Used by tests and status."""
    proc = subprocess.run([ffmpeg_exe(), "-hide_banner", "-i", str(path)],
                          capture_output=True, text=True, encoding="utf-8", errors="replace")
    err = proc.stderr or ""
    info = {"duration": None, "width": None, "height": None, "fps": None, "audio": "Audio:" in err}
    m = _DURATION.search(err)
    if m:
        h, mnt, s = m.groups()
        info["duration"] = int(h) * 3600 + int(mnt) * 60 + float(s)
    v = re.search(r"Video:.*?(\d{2,5})x(\d{2,5})", err)
    if v:
        info["width"], info["height"] = int(v.group(1)), int(v.group(2))
    f = re.search(r"(\d+(?:\.\d+)?) fps", err)
    if f:
        info["fps"] = float(f.group(1))
    return info


def open_encoder(out: Path, width: int, height: int, fps: int, *, crf: int, preset: str) -> subprocess.Popen:
    """An ffmpeg process that encodes raw RGB frames written to its stdin."""
    cmd = [
        ffmpeg_exe(), "-hide_banner", "-loglevel", "error", "-y",
        "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{width}x{height}", "-r", str(fps),
        "-i", "-",
        "-an", "-c:v", "libx264", "-preset", preset, "-crf", str(crf),
        # Cap x264's own threads: render already runs up to cpu_count segment
        # workers in parallel, and an uncapped x264 spawns ~1.5x cores of
        # threads per process -- oversubscription thrash on top of the frame
        # drawing. Let the process pool provide the parallelism.
        "-threads", "2",
        "-pix_fmt", "yuv420p", "-r", str(fps), "-video_track_timescale", str(fps * 512),
        "-movflags", "+faststart",
        str(out),
    ]
    return subprocess.Popen(cmd, stdin=subprocess.PIPE, stderr=subprocess.PIPE)


def close_encoder(proc: subprocess.Popen) -> None:
    try:
        proc.stdin.close()
    except OSError:
        pass
    err = proc.stderr.read().decode("utf-8", "replace") if proc.stderr else ""
    if proc.wait() != 0:
        raise UserError(f"FFmpeg encoding failed:\n{_tail(err)}")
