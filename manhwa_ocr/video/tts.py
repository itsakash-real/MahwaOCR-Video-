"""Text-to-speech narration: turn a chapter's recap script into its audio file.

``video tts --chapter N`` reads the chapter's sentences from
``recaps/chapter_N.cues.json`` (the same sentences ``video align`` will time
against the audio), synthesizes each sentence with Deepgram Aura, joins them
with a short silence, and writes ``audio/chapter_N.m4a`` plus a manifest at
``audio/chapter_N.tts.json``. The rest of the pipeline (pair, align, plan,
render) is unchanged: synthesized narration is a drop-in replacement for a
recording, and because the audio speaks the script exactly, align runs
against the very words it expects.

The Deepgram API key is read from the ``DEEPGRAM_API_KEY`` environment
variable, or from a key file: ``--key-file``, else the ``tts.key_file``
config value, else ``~/.config/manhwa/deepgram.key``. The key lives on the
machine running this command only. It is never logged, never written into
the session folder, and never stored in the repo.

Config (all optional, under a ``tts:`` section):

    tts:
      model: aura-2-thalia-en   # any Deepgram Aura voice model
      gap_seconds: 0.35         # silence between sentences
      key_file: ~/.config/manhwa/deepgram.key

Per-sentence WAVs are cached under ``.work/tts/ch<N>/`` keyed by the
sentence text and the model, so re-running after a one-sentence script edit
only re-synthesizes what changed. ``--force`` re-synthesizes everything.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import urllib.error
import urllib.request
import wave
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from ..util import UserError, atomic_write_text, read_agent_json
from . import ffmpeg
from .media import audio_dir

DEEPGRAM_URL = "https://api.deepgram.com/v1/speak"
DEFAULT_MODEL = "aura-2-thalia-en"
DEFAULT_GAP_SECONDS = 0.35
SAMPLE_RATE = 24000
DEFAULT_KEY_FILE = "~/.config/manhwa/deepgram.key"


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args):  # noqa: ANN002, ANN202 - urllib hook
        raise UserError("Deepgram redirected the request; refusing to follow it with the API key attached.")


# ---------------------------------------------------------------- sentences


def load_sentences(root: Path, chapter: str) -> list[str]:
    """The chapter's narration sentences, from its finalized recap cues."""
    cues_path = Path(root) / "recaps" / f"chapter_{chapter}.cues.json"
    if not cues_path.exists():
        raise UserError(
            f"No recap cues at {cues_path}. Finalize the recap first; "
            "TTS speaks the cues' sentences so the audio matches what align expects."
        )
    data = read_agent_json(cues_path, "Run recap --finalize for this chapter first.")
    sentences = [str(s.get("text", "")).strip() for s in data.get("sentences", [])]
    sentences = [s for s in sentences if s]
    if not sentences:
        raise UserError(f"{cues_path} has no sentences to speak.")
    return sentences


# ---------------------------------------------------------------- API key


def load_api_key(cfg_tts: dict, key_file: str | None = None) -> str:
    """The Deepgram key from the environment or a local key file. Never logged."""
    env = os.environ.get("DEEPGRAM_API_KEY", "").strip()
    if env:
        return env
    candidate = key_file or cfg_tts.get("key_file") or DEFAULT_KEY_FILE
    path = Path(str(candidate)).expanduser()
    if path.exists():
        key = path.read_text(encoding="utf-8").strip()
        if key:
            return key
    raise UserError(
        "No Deepgram API key found. Set the DEEPGRAM_API_KEY environment variable, "
        f"or save the key (and nothing else) in {path}. The key stays on this machine: "
        "it is never written into the session folder or the repo."
    )


# ---------------------------------------------------------------- Deepgram


def deepgram_speak(text: str, model: str, api_key: str, *,
                   opener=None) -> bytes:  # noqa: ANN001 - urllib opener
    """One sentence -> WAV bytes (linear16, mono) from Deepgram's /v1/speak."""
    url = (f"{DEEPGRAM_URL}?model={model}&encoding=linear16"
           f"&sample_rate={SAMPLE_RATE}&container=wav")
    body = json.dumps({"text": text}).encode("utf-8")
    request = urllib.request.Request(url, data=body, method="POST")
    request.add_header("Content-Type", "application/json")
    request.add_header("Authorization", f"Token {api_key}")
    open_fn = opener.open if opener is not None else urllib.request.build_opener(_NoRedirect()).open
    try:
        with open_fn(request, timeout=180) as response:
            audio = response.read()
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            payload = json.loads(exc.read().decode("utf-8", "replace"))
            detail = payload.get("err_msg") or payload.get("message") or ""
        except Exception:  # noqa: BLE001 - error body is best-effort context
            detail = ""
        raise UserError(f"Deepgram TTS failed (HTTP {exc.code}){': ' + detail if detail else ''}. "
                        "Check the API key and the model name.") from None
    except urllib.error.URLError as exc:
        raise UserError(f"Deepgram TTS could not be reached: {exc.reason}") from None
    if not audio:
        raise UserError("Deepgram returned empty audio for a sentence.")
    return audio


# ---------------------------------------------------------------- stitching


def _wav_params_and_frames(wav_bytes: bytes) -> tuple[wave._wave_params, bytes]:
    with wave.open(io.BytesIO(wav_bytes), "rb") as wf:
        return wf.getparams(), wf.readframes(wf.getnframes())


def concat_wavs(wav_list: list[bytes], gap_seconds: float) -> tuple[bytes, list[float]]:
    """Join sentence WAVs with silence. Returns (wav bytes, per-sentence seconds)."""
    if not wav_list:
        raise UserError("Nothing to stitch: no sentence audio.")
    params, first_frames = _wav_params_and_frames(wav_list[0])
    if params.comptype != "NONE" or params.nchannels != 1 or params.sampwidth != 2:
        raise UserError(f"Unexpected WAV format from the TTS provider: {params}")
    gap_frames = int(params.framerate * gap_seconds)
    silence = b"\x00\x00" * gap_frames
    durations = [len(first_frames) / params.sampwidth / params.framerate]
    chunks = [first_frames]
    for wav_bytes in wav_list[1:]:
        other_params, frames = _wav_params_and_frames(wav_bytes)
        if other_params[:3] != params[:3] or other_params.sampwidth != params.sampwidth:
            raise UserError("TTS provider returned inconsistent WAV formats between sentences.")
        chunks.append(silence)
        chunks.append(frames)
        durations.append(len(frames) / params.sampwidth / params.framerate)
    out = io.BytesIO()
    with wave.open(out, "wb") as wf:
        wf.setparams(params)
        for chunk in chunks:
            wf.writeframes(chunk)
    return out.getvalue(), durations


def encode_m4a(wav_path: Path, out_path: Path) -> None:
    ffmpeg.run(["-i", str(wav_path), "-c:a", "aac", "-b:a", "160k", str(out_path)], timeout=600)


# ---------------------------------------------------------------- the command


def synthesize_chapter(root: Path, chapter: str, cfg, *, log: Callable[[str], None],
                       force: bool = False, model: str | None = None,
                       key_file: str | None = None,
                       speak: Callable[[str, str, str], bytes] | None = None) -> Path:
    """Synthesize a chapter's narration into audio/chapter_N.m4a. Returns that path."""
    cfg_tts = {}
    if cfg is not None:
        cfg_tts = (getattr(cfg, "extra", None) or {}).get("tts") or {}
    model = model or cfg_tts.get("model") or DEFAULT_MODEL
    gap = float(cfg_tts.get("gap_seconds", DEFAULT_GAP_SECONDS))
    sentences = load_sentences(root, chapter)
    api_key = load_api_key(cfg_tts, key_file)
    speak = speak or (lambda text, mdl, key: deepgram_speak(text, mdl, key))

    cache_dir = Path(root) / ".work" / "tts" / f"ch{chapter}"
    cache_dir.mkdir(parents=True, exist_ok=True)
    cache_path = cache_dir / "cache.json"
    cache = {}
    if cache_path.exists() and not force:
        try:
            cache = json.loads(cache_path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001 - a broken cache just re-synthesizes
            cache = {}

    wav_list: list[bytes] = []
    total_chars = 0
    for i, text in enumerate(sentences, start=1):
        total_chars += len(text)
        digest = hashlib.sha256(f"{model}\n{text}".encode("utf-8")).hexdigest()
        cached = cache.get(str(i))
        wav_path = cache_dir / f"s{i:03d}.wav"
        if not force and cached and cached.get("sha") == digest and wav_path.exists():
            wav_list.append(wav_path.read_bytes())
            continue
        log(f"  tts {i}/{len(sentences)}: {text[:60]}{'...' if len(text) > 60 else ''}")
        wav_bytes = speak(text, model, api_key)
        wav_path.write_bytes(wav_bytes)
        cache[str(i)] = {"sha": digest}
        wav_list.append(wav_bytes)
    atomic_write_text(cache_path, json.dumps(cache, indent=1))

    joined, durations = concat_wavs(wav_list, gap)
    joined_path = cache_dir / f"chapter_{chapter}.wav"
    joined_path.write_bytes(joined)

    out_dir = audio_dir(root)
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"chapter_{chapter}.m4a"
    encode_m4a(joined_path, out_path)

    starts, t = [], 0.0
    for seconds in durations:
        starts.append(round(t, 3))
        t += seconds + gap
    total_seconds = round(t - gap, 3) if durations else 0.0
    manifest = {
        "version": 1,
        "chapter": str(chapter),
        "provider": "deepgram",
        "model": model,
        "gap_seconds": gap,
        "chars": total_chars,
        "total_seconds": total_seconds,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "sentences": [
            {"n": i + 1, "chars": len(text), "seconds": round(durations[i], 3), "start": starts[i]}
            for i, text in enumerate(sentences)
        ],
    }
    atomic_write_text(out_dir / f"chapter_{chapter}.tts.json", json.dumps(manifest, indent=1))
    log(f"  tts: {len(sentences)} sentences, {total_chars} chars, {total_seconds:.1f}s "
        f"({model}) -> {out_path.relative_to(root)}")
    return out_path
