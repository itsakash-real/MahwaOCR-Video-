"""Image acquisition and slicing.

The important part of this module is `slice_tall_image`.

Webtoon pages are delivered as extremely tall vertical strips -- 900x16000 is
typical. If you hand that to a vision model whole, it gets downscaled to fit the
model's input budget and arrives roughly 110 pixels wide. Text becomes
unreadable and the model starts guessing from context.

Slicing into overlapping, full-resolution tiles is the fix. Snapping each cut to
a low-detail "gutter" row keeps speech bubbles from being bisected.
"""

from __future__ import annotations

import base64
import hashlib
import io
import json
import re
import shutil
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List

import requests
from PIL import Image, UnidentifiedImageError

from .util import UserError, atomic_save_image

# Webtoon strips legitimately exceed Pillow's default guard (~89M px), but a
# finite cap still stops a hostile "image" from exhausting memory.
Image.MAX_IMAGE_PIXELS = 400_000_000

IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".avif"}
SLICE_KEYS = ("enabled", "target_height", "overlap", "snap_to_gutter",
              "gutter_search", "min_height_to_slice", "max_width")


@dataclass
class Tile:
    """One slice of a page, ready to send to a vision model."""

    page_index: int
    tile_index: int
    total_tiles: int
    path: Path
    top: int
    bottom: int
    width: int = 0  # page width after any downscale; tile coords are in this space

    @property
    def label(self) -> str:
        if self.total_tiles == 1:
            return f"page {self.page_index}"
        return f"page {self.page_index} (slice {self.tile_index}/{self.total_tiles})"


def natural_key(path: Path):
    """'page_2' before 'page_10'. Plain sorting would put 10 first."""
    return [int(t) if t.isdigit() else t.casefold() for t in re.split(r"(\d+)", path.name)]


def list_images(folder: Path) -> List[Path]:
    if not folder.is_dir():
        raise UserError(f"Folder not found: {folder}")
    return sorted(
        (p for p in folder.iterdir() if p.is_file() and p.suffix.lower() in IMAGE_SUFFIXES),
        key=natural_key,
    )


def import_pages(sources: List[Path], dest: Path, log=print) -> List[Path]:
    """Copy user-supplied images into dest as page_001.png, page_002.png, ...

    Gives --folder chapters the same layout as downloaded ones, so later steps
    (and video, which keeps the pages) never care where a chapter came from.
    """
    dest.mkdir(parents=True, exist_ok=True)
    out: List[Path] = []
    for i, src in enumerate(sources, start=1):
        target = dest / f"page_{i:03d}.png"
        if not (target.exists() and _is_readable(target)):
            try:
                with Image.open(src) as img:
                    atomic_save_image(img.convert("RGB"), target)
            except (OSError, UnidentifiedImageError) as exc:
                raise UserError(f"Can't read image {src.name}: {exc}") from None
        out.append(target)
    # A re-run with fewer images must not leave stale extra pages behind.
    for extra in dest.glob("page_*.png"):
        if extra not in out:
            extra.unlink()
    return out


def _is_readable(path: Path) -> bool:
    try:
        with Image.open(path) as img:
            img.verify()
        return True
    except (OSError, UnidentifiedImageError, SyntaxError, ValueError):
        return False


def download_images(
    urls: Iterable[str],
    dest: Path,
    *,
    referer: str | None = None,
    config: Dict[str, Any] | None = None,
    log=print,
) -> List[Path]:
    """Fetch pages and normalize them to PNG. Reuses pages already on disk."""
    cfg = config or {}
    dest.mkdir(parents=True, exist_ok=True)
    headers = dict(cfg.get("headers") or {})
    if referer:
        headers.setdefault("Referer", referer)

    timeout = cfg.get("timeout", 60)
    retries = max(1, int(cfg.get("max_retries", 3)))
    delay = cfg.get("delay", 0.25)

    paths: List[Path] = []
    for i, url in enumerate(urls, start=1):
        out = dest / f"page_{i:03d}.png"
        # A page left half-written by a crash must not be trusted next time.
        if out.exists() and _is_readable(out):
            paths.append(out)
            continue

        last_err: Exception | None = None
        for attempt in range(retries):
            try:
                resp = requests.get(url, headers=headers, timeout=timeout)
                resp.raise_for_status()
                ctype = resp.headers.get("Content-Type", "")
                if ctype.startswith("text/html"):
                    raise ValueError("server returned a web page, not an image (blocked or expired link?)")
                with Image.open(io.BytesIO(resp.content)) as img:
                    rgb = img.convert("RGB")
                atomic_save_image(rgb, out)
                paths.append(out)
                log(f"  page {i:>3}  {rgb.width}x{rgb.height}")
                break
            except Exception as exc:  # noqa: BLE001 - surfaced below
                last_err = exc
                status = getattr(getattr(exc, "response", None), "status_code", None)
                if status in (401, 403, 404, 410):
                    break  # retrying won't help
                time.sleep(1.5 * (attempt + 1))
        else:
            last_err = last_err or RuntimeError("unknown error")
        if len(paths) < i:
            raise UserError(
                f"Couldn't download page {i} of this chapter ({last_err}).\n"
                "If the site blocks scripts, save the images with your browser and use --folder."
            )

        if delay:
            time.sleep(delay)

    return paths


def _row_detail(img: Image.Image, y: int, width: int) -> float:
    """Rough busy-ness score for one pixel row. Low means a flat gutter."""
    # tobytes() on an L-mode row yields one byte per pixel. Stable across
    # Pillow versions, and faster than getdata() for this hot loop.
    pixels = img.crop((0, y, width, y + 1)).convert("L").tobytes()
    if not pixels:
        return 0.0
    mean = sum(pixels) / len(pixels)
    return sum(abs(p - mean) for p in pixels) / len(pixels)


def _find_gutter(img: Image.Image, target_y: int, search: int, width: int) -> int:
    """Find the flattest row near target_y, so cuts land between panels."""
    height = img.height
    lo = max(1, target_y - search)
    hi = min(height - 1, target_y + search)
    if lo >= hi or search <= 0:
        return target_y

    best_y, best_score = target_y, float("inf")
    step = max(1, (hi - lo) // 120)
    for y in range(lo, hi, step):
        score = _row_detail(img, y, width)
        # Prefer flat rows, with a mild pull toward the requested position so we
        # don't drift far from the target tile height.
        score += abs(y - target_y) / search * 1.5
        if score < best_score:
            best_y, best_score = y, score
    return best_y


def _settings(cfg: Dict[str, Any]) -> Dict[str, Any]:
    s = {
        "enabled": bool(cfg.get("enabled", True)),
        "target_height": max(400, int(cfg.get("target_height", 1400))),
        "overlap": max(0, int(cfg.get("overlap", 180))),
        "snap_to_gutter": bool(cfg.get("snap_to_gutter", True)),
        "gutter_search": max(0, int(cfg.get("gutter_search", 220))),
        "min_height_to_slice": int(cfg.get("min_height_to_slice", 1800)),
        "max_width": int(cfg.get("max_width", 1100) or 0),
    }
    # An overlap at or above half the tile height would make little or no
    # forward progress per tile; cap it so slicing always terminates sensibly.
    s["overlap"] = min(s["overlap"], s["target_height"] // 3)
    return s


def slice_tall_image(
    path: Path,
    page_index: int,
    out_dir: Path,
    *,
    config: Dict[str, Any] | None = None,
) -> List[Tile]:
    """Cut a page into overlapping full-resolution tiles."""
    s = _settings(config or {})
    out_dir.mkdir(parents=True, exist_ok=True)

    try:
        src = Image.open(path)
    except (OSError, UnidentifiedImageError) as exc:
        raise UserError(f"Can't read image {path.name}: {exc}") from None

    with src:
        img = src.convert("RGB")

    if s["max_width"] and img.width > s["max_width"]:
        ratio = s["max_width"] / img.width
        img = img.resize((s["max_width"], max(1, int(img.height * ratio))), Image.LANCZOS)
    width, height = img.size

    if not s["enabled"] or height <= s["min_height_to_slice"]:
        single = out_dir / f"p{page_index:03d}_t01.png"
        if not (single.exists() and _is_readable(single)):
            atomic_save_image(img, single)
        return [Tile(page_index, 1, 1, single, 0, height, width)]

    target, overlap = s["target_height"], s["overlap"]
    bounds: List[tuple[int, int]] = []
    top = 0
    while top < height:
        desired = min(top + target, height)
        if desired >= height - 200:
            bounds.append((top, height))
            break
        cut = _find_gutter(img, desired, s["gutter_search"], width) if s["snap_to_gutter"] else desired
        cut = max(cut, top + target // 2)  # guard against pathological snaps
        bounds.append((top, cut))
        top = max(cut - overlap, top + 1)

    tiles: List[Tile] = []
    total = len(bounds)
    for idx, (t, b) in enumerate(bounds, start=1):
        tile_path = out_dir / f"p{page_index:03d}_t{idx:02d}.png"
        if not (tile_path.exists() and _is_readable(tile_path)):
            atomic_save_image(img.crop((0, t, width, b)), tile_path)
        tiles.append(Tile(page_index, idx, total, tile_path, t, b, width))
    return tiles


def build_tiles(
    pages: List[Path],
    out_dir: Path,
    *,
    config: Dict[str, Any] | None = None,
    log=print,
) -> List[Tile]:
    """Slice every page in a chapter.

    Tiles are cached on disk by filename, so if the slicing settings or the
    page set change, the old tiles are wrong. A fingerprint file detects that
    and clears them instead of silently reusing mismatched slices.
    """
    settings = _settings(config or {})
    fingerprint = hashlib.sha256(
        json.dumps(
            {"settings": settings, "pages": [(p.name, p.stat().st_size) for p in pages]},
            sort_keys=True,
        ).encode()
    ).hexdigest()[:16]
    marker = out_dir / ".fingerprint"
    if out_dir.exists() and (not marker.exists() or marker.read_text().strip() != fingerprint):
        shutil.rmtree(out_dir, ignore_errors=True)
    out_dir.mkdir(parents=True, exist_ok=True)

    tiles: List[Tile] = []
    for i, page in enumerate(pages, start=1):
        page_tiles = slice_tall_image(page, i, out_dir, config=settings)
        tiles.extend(page_tiles)
        if len(page_tiles) > 1:
            log(f"  page {i:>3} -> {len(page_tiles)} slices")
    marker.write_text(fingerprint)
    return tiles


def image_hash(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()[:24]


def encode_image(path: Path) -> tuple[str, str]:
    """Return (base64_data, media_type) for a tile."""
    return base64.standard_b64encode(path.read_bytes()).decode("ascii"), "image/png"
