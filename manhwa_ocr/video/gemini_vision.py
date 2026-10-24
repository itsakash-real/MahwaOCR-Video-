"""Gemini Vision for placed prep: trim decisions + placement QC.

Why this module lives in ``manhwa_ocr/video/`` (and not in the generic
``providers.py``): the transcript pipeline's ``GeminiProvider`` is a
single-env-key vision/text backend whose model + prompt version are part
of the tile cache fingerprint. This step is different machinery — it is
a video-prep/QC tool, in the same family as ``tts.py`` (the Deepgram
provider that also lives here, reads its key from a local file at call
time, and never logs or stores it):

  * THREE key files (``~/.config/manhwa/gemini-{1,2,3}.key``), read at
    call time, rotated across calls and across retries. A key value is
    never logged, never written into any artifact, and never included
    in an error message (errors carry only an HTTP status / error type).
  * JSON-mode ``generateContent`` calls carrying several labelled
    images in one request (storyboard-style grouping, ~6 per call).
  * Sequential pacing only — free tier, no parallel bursts.

Two consumers:

  * ``video vision-trim`` -> :func:`vision_trim_chapter` asks Gemini, per
    batch of placed units (panels + B1f slice bands, in story order),
    for each unit's art bounding box under the user's rule (no white
    space around the art; the art must connect to the top and the
    bottom). Every vision box is sanity-clamped against the algorithmic
    ink bounds (``plan.placed_trim_rect``): a box that would cut ink the
    detector found is rejected, and any failure (API, parse, validation,
    clamp) falls back to the algorithmic trim for that unit, flagged.
    The chapter NEVER blocks on the API. Decisions are auditable in
    ``.work/video/ch<N>/vision_trim.json`` and ``plan.build_plan``
    consumes them through the same ``trim`` spec the algorithmic trim
    uses.

  * :func:`qc_frames` — the placement QC helper the build phase calls
    on sampled rendered frames: per frame, does it have white space
    around the art, is the panel floating (instead of centred and
    touching top/bottom, or left/right when wide), and are there
    letterbox bands? A frame that cannot be judged is ``"unknown"``,
    never silently ``"clean"``.
"""

from __future__ import annotations

import base64
import io
import json
import math
import random
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Sequence, Tuple

import requests
from PIL import Image

from ..util import UserError, atomic_write_text, extract_json, require_chapter
from ..workspace import work_root

# ------------------------------------------------------------------ pins
# Pinned constants (mirrored in config.py's ``video.gemini_vision``
# defaults and asserted equal by tests — the values must never live
# only in an agent's head).

GEMINI_VISION_MODEL = "gemini-2.5-flash"   # vision-capable, on the keys' verified list
GEMINI_BASE_URL = "https://generativelanguage.googleapis.com/v1beta"
GEMINI_KEY_FILES = ("~/.config/manhwa/gemini-1.key",
                    "~/.config/manhwa/gemini-2.key",
                    "~/.config/manhwa/gemini-3.key")
GEMINI_BATCH_SIZE = 6          # labelled images per call (storyboard grouping)
GEMINI_MAX_ATTEMPTS = 6        # bounded TOTAL attempts per call, across keys
GEMINI_BACKOFF_BASE = 1.0      # seconds; grows 2**n with jitter on 429/5xx
GEMINI_BACKOFF_MAX = 30.0      # seconds; one backoff sleep never exceeds this
GEMINI_PACE_SECONDS = 1.0      # minimum seconds between successive calls
GEMINI_TIMEOUT = 180           # seconds per HTTP attempt
MIN_BOX_SIDE = 0.05            # a vision box narrower/shorter than this is degenerate

Box = Tuple[float, float, float, float]


class GeminiVisionError(RuntimeError):
    """A typed Gemini failure. Callers convert it to fallback verdicts.

    Messages carry only an HTTP status code or an exception type name —
    never a key value, never request headers, never a request body."""


class _Retryable(Exception):
    """One attempt failed in a way another key / a backoff may fix."""


# ------------------------------------------------------------------ images


def _mime_for(path: Path) -> str:
    return {".jpg": "image/jpeg", ".jpeg": "image/jpeg",
            ".webp": "image/webp"}.get(path.suffix.lower(), "image/png")


def image_part(image: Any) -> Dict[str, Any]:
    """One Gemini ``inlineData`` part from a PIL image, a path, or bytes."""
    if isinstance(image, (str, Path)):
        p = Path(image)
        data, mime = p.read_bytes(), _mime_for(p)
    elif isinstance(image, Image.Image):
        buf = io.BytesIO()
        image.convert("RGB").save(buf, "PNG")
        data, mime = buf.getvalue(), "image/png"
    elif isinstance(image, (bytes, bytearray)):
        data, mime = bytes(image), "image/png"
    else:
        raise GeminiVisionError(f"unsupported image type for Gemini part: {type(image).__name__}")
    return {"inlineData": {"mimeType": mime,
                           "data": base64.standard_b64encode(data).decode("ascii")}}


def _text_part(text: str) -> Dict[str, Any]:
    return {"text": text}


# ------------------------------------------------------------------ client


def _requests_transport(url: str, headers: Dict[str, str],
                        payload: Dict[str, Any], timeout: float):
    return requests.post(url, headers=headers, json=payload, timeout=timeout)


class GeminiVisionClient:
    """JSON-mode generateContent with multi-image parts, key rotation,
    jittered backoff on 429/5xx, and sequential pacing.

    ``transport`` is injectable for tests: a callable
    ``(url, headers, payload, timeout) -> response`` where the response
    exposes ``status_code`` and ``json()``. Tests mock it — no test ever
    makes a real API call. ``sleep`` / ``clock`` / ``rng`` are injectable
    for the same reason.
    """

    def __init__(self, *, model: str | None = None,
                 key_files: Sequence[str | Path] | None = None,
                 max_attempts: int = GEMINI_MAX_ATTEMPTS,
                 pace_seconds: float = GEMINI_PACE_SECONDS,
                 backoff_base: float = GEMINI_BACKOFF_BASE,
                 timeout: float = GEMINI_TIMEOUT,
                 base_url: str = GEMINI_BASE_URL,
                 transport: Callable | None = None,
                 sleep: Callable[[float], None] = time.sleep,
                 clock: Callable[[], float] = time.monotonic,
                 rng: random.Random | None = None):
        self.model = model or GEMINI_VISION_MODEL
        self.key_files = [Path(str(p)).expanduser()
                          for p in (key_files or GEMINI_KEY_FILES)]
        self.max_attempts = max(1, int(max_attempts))
        self.pace_seconds = max(0.0, float(pace_seconds))
        self.backoff_base = max(0.0, float(backoff_base))
        self.timeout = float(timeout)
        self.base_url = str(base_url).rstrip("/")
        self._transport = transport or _requests_transport
        self._sleep = sleep
        self._clock = clock
        self._rng = rng or random.Random()
        self._next_key = 0
        self._last_call_at: float | None = None

    # -- keys: read at call time, values never leave this object ----

    def _load_keys(self) -> List[str]:
        keys: List[str] = []
        for path in self.key_files:
            try:
                value = path.read_text(encoding="utf-8").strip()
            except OSError:
                value = ""
            if value:
                keys.append(value)
        if not keys:
            raise GeminiVisionError(
                "no Gemini API key found in any key file "
                f"({', '.join(str(p) for p in self.key_files)}); "
                "the key stays on this machine and is never stored in the repo")
        return keys

    # -- pacing: sequential calls, a minimum gap between them --------

    def _pace(self) -> None:
        now = self._clock()
        if self._last_call_at is not None and self.pace_seconds > 0:
            wait = self.pace_seconds - (now - self._last_call_at)
            if wait > 0:
                self._sleep(wait)
                now = self._clock()
        self._last_call_at = now

    def _backoff(self, attempt_index: int) -> None:
        delay = min(GEMINI_BACKOFF_MAX, self.backoff_base * (2 ** attempt_index))
        self._sleep(delay * (0.75 + 0.5 * self._rng.random()))

    # -- one JSON call ------------------------------------------------

    def generate_json(self, parts: List[Dict[str, Any]]) -> Any:
        """POST one generateContent call (JSON response mode) and return
        the parsed JSON payload. Rotates keys per call; on HTTP 429/5xx
        (or a transport/parse failure) backs off with jitter and rotates
        to the next key. Raises :class:`GeminiVisionError` after
        ``max_attempts`` total attempts — the message names only the
        last HTTP status / error type."""
        keys = self._load_keys()
        self._pace()
        start = self._next_key % len(keys)
        self._next_key = (start + 1) % len(keys)
        url = f"{self.base_url}/models/{self.model}:generateContent"
        payload = {
            "contents": [{"role": "user", "parts": parts}],
            "generationConfig": {"temperature": 0.0,
                                 "responseMimeType": "application/json"},
        }
        last = "no attempt was made"
        for attempt in range(self.max_attempts):
            key = keys[(start + attempt) % len(keys)]
            try:
                resp = self._transport(
                    url, {"content-type": "application/json",
                          "x-goog-api-key": key}, payload, self.timeout)
                status = int(getattr(resp, "status_code", 0) or 0)
                if status == 429 or status >= 500:
                    raise _Retryable(f"HTTP {status}")
                if status != 200:
                    raise GeminiVisionError(f"Gemini request failed (HTTP {status})")
                try:
                    data = resp.json()
                except Exception:  # noqa: BLE001 - an unparseable body is retryable
                    raise _Retryable("unparseable response body") from None
                chunks = ((data.get("candidates") or [{}])[0].get("content") or {}).get("parts") or []
                text = "".join(str(c.get("text", "")) for c in chunks if isinstance(c, dict))
                if not text.strip():
                    raise _Retryable("empty model response")
                try:
                    return json.loads(text)
                except json.JSONDecodeError:
                    return extract_json(text)
            except GeminiVisionError:
                raise
            except _Retryable as exc:
                last = str(exc)
            except Exception as exc:  # noqa: BLE001 - transport failures rotate too
                last = f"{type(exc).__name__}"
            if attempt + 1 < self.max_attempts:
                self._backoff(attempt)
        raise GeminiVisionError(
            f"Gemini request failed after {self.max_attempts} attempts (last: {last})")


def client_from_vision_cfg(vcfg: Dict[str, Any] | None, **overrides: Any) -> GeminiVisionClient:
    """A client from a ``video.gemini_vision`` config section (or pins)."""
    cfg = dict(vcfg or {})
    kwargs: Dict[str, Any] = {
        "model": cfg.get("model") or GEMINI_VISION_MODEL,
        "key_files": cfg.get("key_files") or GEMINI_KEY_FILES,
        "max_attempts": int(cfg.get("max_attempts") or GEMINI_MAX_ATTEMPTS),
    }
    kwargs.update({k: v for k, v in overrides.items() if v is not None})
    return GeminiVisionClient(**kwargs)


# ------------------------------------------------------------------ boxes


def validate_vision_box(raw: Any) -> Box | None:
    """A vision box, or None: all four numbers present and finite,
    ``0 <= x0 < x1 <= 1``, ``0 <= y0 < y1 <= 1``, and neither side
    degenerate (width and height at least ``MIN_BOX_SIDE``)."""
    if not isinstance(raw, (list, tuple)) or len(raw) != 4:
        return None
    vals: List[float] = []
    for v in raw:
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            return None
        f = float(v)
        if not math.isfinite(f):
            return None
        vals.append(f)
    x0, y0, x1, y1 = vals
    if not (0.0 <= x0 < x1 <= 1.0 and 0.0 <= y0 < y1 <= 1.0):
        return None
    if (x1 - x0) < MIN_BOX_SIDE or (y1 - y0) < MIN_BOX_SIDE:
        return None
    return (x0, y0, x1, y1)


def vision_box_cuts_ink(box: Box, ink: Box, tol: float = 1e-4) -> bool:
    """True when `box` excludes ink the detector found: the vision box
    is strictly inside the ink bounds on any side."""
    return (box[0] > ink[0] + tol or box[1] > ink[1] + tol
            or box[2] < ink[2] - tol or box[3] < ink[3] - tol)


# ------------------------------------------------------------------ trim

TRIM_PROMPT = (
    "You are the vision trim editor for a manhwa recap video. "
    "Panels must be cut so NO white space remains around the art; "
    "the art must connect to the top and the bottom. "
    "For EACH labeled panel below, return the art bounding box as "
    "fractions {x0, y0, x1, y1} of that panel's own image: the smallest "
    "rectangle that contains all of the art and no surrounding white "
    "space. Return ONLY a strict JSON object mapping each unit id to "
    "[x0, y0, x1, y1], with no other text."
)


def _round_box(box: Sequence[float]) -> List[float]:
    return [round(float(v), 4) for v in box]


def run_vision_trim(units: List[Dict[str, Any]], client: Any, *,
                    ink_rect_fn: Callable[[Image.Image], Box | None] | None = None,
                    batch_size: int = GEMINI_BATCH_SIZE,
                    model: str | None = None) -> Dict[str, Any]:
    """Decide every unit's trim. NEVER raises for an API/parse failure:
    the affected units fall back to the algorithmic trim, flagged.

    `units` are ``{"id": str, ...}`` in story order, each carrying its
    image either decoded (``"image"``: a PIL image) or as a zero-arg
    ``"load"`` callable returning one. Images are resolved ONE BATCH
    at a time and released before the next batch loads — a chapter's
    panels are never all decoded at once (183 full-size panels held
    simultaneously OOM-killed the first real run of this step).
    Returns the artifact document (see :func:`vision_trim_chapter` for
    its on-disk home); each unit entry is
    ``{"box": [x0, y0, x1, y1], "source": "gemini"|"fallback",
    "model": str, "reason": str (fallback only)}`` where a fallback
    entry's box is the algorithmic ink rect (or the full unit when the
    detector found no art bounds)."""
    if ink_rect_fn is None:
        from .plan import placed_trim_rect as ink_rect_fn  # lazy: plan imports this module
    model = model or getattr(client, "model", None) or GEMINI_VISION_MODEL
    batch_size = max(1, int(batch_size or GEMINI_BATCH_SIZE))
    entries: Dict[str, Any] = {}

    def fallback(unit: Dict[str, Any], reason: str,
                 img: Any = None, ink: Box | None = ...) -> None:  # type: ignore[assignment]
        if ink is ...:  # type: ignore[comparison-overlap]
            try:
                ink = ink_rect_fn(img) if img is not None else None
            except Exception:  # noqa: BLE001 - a detector failure is still a fallback
                ink = None
        box = _round_box(ink) if ink is not None else [0.0, 0.0, 1.0, 1.0]
        entries[unit["id"]] = {"box": box, "source": "fallback",
                                "model": model, "reason": reason}

    for start in range(0, len(units), batch_size):
        batch = units[start:start + batch_size]
        # Resolve this batch's images only; they go out of scope at the
        # end of the iteration, before the next batch is loaded.
        loaded: List[Tuple[Dict[str, Any], Any]] = []
        for unit in batch:
            try:
                img = unit["load"]() if callable(unit.get("load")) else unit["image"]
            except Exception:  # noqa: BLE001 - one bad panel must not kill the run
                img = None
            if img is None:
                fallback(unit, "image_load_failed")
            else:
                loaded.append((unit, img))
        if loaded:
            parts: List[Dict[str, Any]] = [_text_part(TRIM_PROMPT)]
            for unit, img in loaded:
                parts.append(_text_part(f"Panel {unit['id']}:"))
                parts.append(image_part(img))
            call_failed = False
            try:
                raw = client.generate_json(parts)
            except Exception:  # noqa: BLE001 - the chapter never blocks on the API
                raw, call_failed = None, True
            if not isinstance(raw, dict):
                raw = {}
            for unit, img in loaded:
                box = validate_vision_box(raw.get(unit["id"]))
                if box is None:
                    fallback(unit, "gemini_failure" if call_failed
                             else "invalid_or_missing_box", img=img)
                    continue
                try:
                    ink = ink_rect_fn(img)
                except Exception:  # noqa: BLE001
                    ink = None
                if ink is None:
                    fallback(unit, "no_detector_ink_bounds", img=img, ink=None)
                    continue
                if vision_box_cuts_ink(box, tuple(float(v) for v in ink)):
                    fallback(unit, "vision_box_cuts_detector_ink", img=img,
                             ink=tuple(float(v) for v in ink))
                    continue
                entries[unit["id"]] = {"box": _round_box(box), "source": "gemini",
                                       "model": model}
        del loaded
    gemini_n = sum(1 for e in entries.values() if e["source"] == "gemini")
    return {"version": 1, "kind": "vision_trim", "model": model,
            "batch_size": batch_size,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "units": entries,
            "summary": {"total": len(entries), "gemini": gemini_n,
                        "fallback": len(entries) - gemini_n}}


def enumerate_placed_units(root: Path, chapter: str) -> List[Dict[str, Any]]:
    """A chapter's placed units in story order: every panel from
    panels.json, expanded through the cues' B1f slice hints exactly as
    the planner expands them — a hinted panel contributes one unit per
    kept band (the band's own cropped image, id = the slice id), an
    unhinted panel contributes itself."""
    from . import cues as cues_mod
    from .panels import load_panels, panel_image
    from .slicehints import kept_bands, slice_id

    chapter = require_chapter(chapter)
    data = load_panels(root, chapter)
    if not data:
        raise UserError(f"No panels.json for chapter {chapter}; run video panels first.")
    hints = cues_mod.slice_hints_of(cues_mod.load_cues(root, chapter))
    norm_w = data.get("norm_width")
    fp = data.get("fingerprint")

    def loader(panel: Dict[str, Any], band: Tuple[float, float] | None):
        def load() -> Image.Image:
            img = panel_image(root, chapter, panel, norm_w, clean=True,
                              fingerprint=fp)
            if band is not None:
                y0 = int(round(band[0] * img.height))
                y1 = int(round(band[1] * img.height))
                if 0 <= y0 < y1 <= img.height:
                    img = img.crop((0, y0, img.width, y1))
            return img
        return load

    units: List[Dict[str, Any]] = []
    for panel in data["panels"]:
        pid = panel["id"]
        spec = hints.get(pid)
        bands = kept_bands(spec) if spec else [(0.0, 1.0)]
        if len(bands) == 1 and bands[0] == (0.0, 1.0):
            units.append({"id": pid, "panel": pid, "load": loader(panel, None)})
            continue
        for k, (f0, f1) in enumerate(bands):
            units.append({"id": slice_id(pid, k), "panel": pid,
                          "slice": [f0, f1], "load": loader(panel, (f0, f1))})
    return units


def vision_trim_path(root: Path, chapter: str) -> Path:
    return work_root(root) / "video" / f"ch{require_chapter(chapter)}" / "vision_trim.json"


def load_vision_trim(root: Path, chapter: str) -> Dict[str, Any] | None:
    """The artifact's per-unit entries ({unit id: entry}), or None when
    no usable artifact exists — planning then runs purely algorithmic,
    exactly as before this step existed."""
    try:
        data = json.loads(vision_trim_path(root, chapter).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    units = data.get("units") if isinstance(data, dict) else None
    return units if isinstance(units, dict) and units else None


def vision_trim_chapter(root: Path, chapter: str, *, client: Any = None,
                        vcfg: Dict[str, Any] | None = None,
                        batch_size: int | None = None,
                        log: Callable[[str], None] | None = None) -> Dict[str, Any]:
    """``video vision-trim``: decide + write ``vision_trim.json`` for a
    chapter. Safe to re-run — it overwrites its own artifact. Never
    raises for a Gemini failure: those units are written as fallback."""
    chapter = require_chapter(chapter)
    units = enumerate_placed_units(root, chapter)
    cfg = dict(vcfg or {})
    client = client or client_from_vision_cfg(cfg)
    doc = run_vision_trim(
        units, client,
        batch_size=int(batch_size or cfg.get("batch_size") or GEMINI_BATCH_SIZE),
        model=cfg.get("model") or getattr(client, "model", None) or GEMINI_VISION_MODEL)
    doc = {"chapter": chapter, **doc}
    path = vision_trim_path(root, chapter)
    atomic_write_text(path, json.dumps(doc, indent=1, ensure_ascii=False))
    if log:
        s = doc["summary"]
        log(f"  chapter {chapter}: vision trim for {s['total']} unit(s): "
            f"{s['gemini']} gemini, {s['fallback']} fallback -> {path}")
    return doc


# ------------------------------------------------------------------ QC

QC_PROMPT = (
    "You are the placement QC checker for a manhwa recap video. "
    "For EACH labeled frame below, check: "
    "(a) is there white space around the art in this frame? "
    "(b) is the panel floating, or is it centered and touching the top "
    "and bottom (or left and right if the panel is wide)? "
    "(c) are there any letterbox bands? "
    "Return ONLY a strict JSON object mapping each frame id to "
    "{\"white_space\": bool, \"floating\": bool, \"bands\": bool, "
    "\"note\": string}, with no other text."
)

QC_UNKNOWN = {"verdict": "unknown", "white_space": None, "floating": None,
              "bands": None}


def _frame_meta(frame: Any, index: int) -> Tuple[str, Any, Any]:
    """(id, timestamp, image) from a frame dict / (id, image) tuple."""
    if isinstance(frame, dict):
        fid = frame.get("id") or frame.get("shot_id") or frame.get("shot") or f"F{index + 1}"
        ts = frame.get("timestamp", frame.get("t"))
        img = frame.get("image", frame.get("path"))
        return str(fid), ts, img
    if isinstance(frame, (list, tuple)) and len(frame) >= 2:
        return str(frame[0]), None, frame[1]
    return f"F{index + 1}", None, frame


def qc_frames(frames: Sequence[Any], client: Any = None, *,
              batch_size: int = GEMINI_BATCH_SIZE,
              vcfg: Dict[str, Any] | None = None) -> List[Dict[str, Any]]:
    """Placement QC for sampled frames. Returns one verdict per frame,
    in input order: ``{"id", "timestamp", "verdict", "white_space",
    "floating", "bands", "note"}`` where verdict is ``"clean"`` (all
    three checks false), ``"issues"`` (any check true) or ``"unknown"``
    (that frame could not be judged — a client failure, a missing or
    malformed entry; never silently "clean")."""
    client = client or client_from_vision_cfg(vcfg)
    batch_size = max(1, int(batch_size or GEMINI_BATCH_SIZE))
    metas = [_frame_meta(f, i) for i, f in enumerate(frames)]
    verdicts: Dict[str, Dict[str, Any]] = {}

    def unknown(fid: str, ts: Any, note: str) -> Dict[str, Any]:
        return {"id": fid, "timestamp": ts, **QC_UNKNOWN, "note": note}

    for start in range(0, len(metas), batch_size):
        batch = metas[start:start + batch_size]
        parts: List[Dict[str, Any]] = [_text_part(QC_PROMPT)]
        ok = True
        for fid, _ts, img in batch:
            parts.append(_text_part(f"Frame {fid}:"))
            try:
                parts.append(image_part(img))
            except Exception:  # noqa: BLE001 - an unreadable frame is unknown, not clean
                ok = False
        raw: Any = None
        if ok:
            try:
                raw = client.generate_json(parts)
            except Exception:  # noqa: BLE001 - QC never blocks a build on the API
                raw = None
        if not isinstance(raw, dict):
            raw = {}
        for fid, ts, _img in batch:
            entry = raw.get(fid)
            if (not isinstance(entry, dict)
                    or not all(isinstance(entry.get(k), bool)
                               for k in ("white_space", "floating", "bands"))):
                verdicts[fid] = unknown(fid, ts, "no parseable QC verdict for this frame")
                continue
            flagged = bool(entry["white_space"] or entry["floating"] or entry["bands"])
            verdicts[fid] = {
                "id": fid, "timestamp": ts,
                "verdict": "issues" if flagged else "clean",
                "white_space": entry["white_space"],
                "floating": entry["floating"],
                "bands": entry["bands"],
                "note": str(entry.get("note") or ""),
            }
    return [verdicts[fid] for fid, _ts, _img in metas]
