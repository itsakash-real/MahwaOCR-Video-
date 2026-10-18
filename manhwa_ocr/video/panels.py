"""Split a chapter's pages into panels.

Webtoon "pages" are arbitrary cuts of one long vertical strip, so a panel can
start on page 3 and end on page 4. Pages are therefore treated as one
continuous strip, normalised to a common width, and split at gutters: runs of
uniform rows that separate panels.

Only a thin summary of each row (its mean brightness and how uniform it is) is
kept for the whole chapter, never the full stitched image, which for a
30-page chapter would exceed Pillow's pixel limit.

Panels are stored as boxes in panels.json. Image crops are produced on demand
from the kept page images, so nothing is duplicated on disk.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, List

import numpy as np
from PIL import Image, ImageDraw, ImageFont

from ..util import UserError, atomic_save_image, atomic_write_text, require_chapter
from ..workspace import work_root
from .media import list_pages, media_dir

PANELS_VERSION = 4
ANALYSIS_WIDTH = 240     # rows are summarised at this width; plenty for gutter finding
MAX_PANEL_RATIO = 3.2    # height/width beyond which a gutterless stretch gets split
TALL_RATIO = 1.6         # height/width at which a panel scrolls instead of zooming
SHEET_PER_PAGE = 12
WHITE_THRESHOLD = 242    # a row whose pixels are (almost) all brighter than this is white gutter
BLACK_THRESHOLD = 13     # ... or all darker than this, near-black gutter
EDGE_ROW_SHARE = 0.985   # share of a row's pixels that must be white/black for it to count


@dataclass
class Panel:
    id: str
    index: int
    y0: float            # global strip coordinates, at the normalised width
    y1: float
    x0: float
    x1: float
    segments: List[Dict[str, Any]] = field(default_factory=list)
    tall: bool = False
    scene: str = ""
    text: str = ""

    @property
    def height(self) -> float:
        return self.y1 - self.y0

    @property
    def width(self) -> float:
        return self.x1 - self.x0


# ------------------------------------------------------------------ settings


def _settings(cfg: Dict[str, Any] | None) -> Dict[str, Any]:
    cfg = cfg or {}
    return {
        "gutter_min": max(4, int(cfg.get("gutter_min", 24))),
        "flat_threshold": float(cfg.get("flat_threshold", 4.0)),
        "gutter_drift": float(cfg.get("gutter_drift", 8.0)),
        "drop_below": max(0, int(cfg.get("drop_below", 80))),
        "merge_below": max(0, int(cfg.get("merge_below", 220))),
        "skip_landscape": bool(cfg.get("skip_landscape", True)),
        # Edge tightening: after a panel's bounds are found, rows at its top and bottom
        # that are all white (gray > white_threshold) or all near-black (gray <
        # black_threshold) are manhwa gutter, not artwork, and are trimmed off.
        "tighten_edges": bool(cfg.get("tighten_edges", True)),
        "white_threshold": min(255, max(128, int(cfg.get("white_threshold", WHITE_THRESHOLD)))),
        "black_threshold": min(127, max(0, int(cfg.get("black_threshold", BLACK_THRESHOLD)))),
    }


# ------------------------------------------------------------------ strip analysis


@dataclass
class _Page:
    index: int           # 1-based position in the chapter
    path: Path
    w: int
    h: int
    offset: float = 0.0  # global y where this page starts (normalised units)
    norm_h: float = 0.0


def _is_flat_banner(path: Path) -> bool:
    """Landscape credit banners are near-uniform; story spreads are not."""
    try:
        with Image.open(path) as img:
            w, h = img.size
            if h >= 600:
                small = img.convert("L").resize((60, max(4, round(60 * h / max(1, w)))), Image.BILINEAR)
                import numpy as _np

                g = _np.asarray(small, dtype=_np.float32)
                if float(g.std()) >= 12.0:
                    return False  # detailed art: a story spread, keep it
                return True
            return True
    except OSError:
        return True


def _row_stats(path: Path, norm_w: int) -> tuple[np.ndarray, np.ndarray]:
    """Per-row mean brightness and non-uniformity, at normalised height."""
    with Image.open(path) as img:
        w, h = img.size
        rows = max(1, round(h * norm_w / w))
        g = np.asarray(img.convert("L").resize((ANALYSIS_WIDTH, rows), Image.BILINEAR), dtype=np.float32)
    mean = g.mean(axis=1)
    dev = np.abs(g - mean[:, None]).mean(axis=1)
    return mean, dev


def _runs(mask: np.ndarray) -> List[tuple[int, int]]:
    """[start, end) runs of True."""
    if mask.size == 0:
        return []
    padded = np.concatenate(([False], mask, [False]))
    edges = np.flatnonzero(padded[1:] != padded[:-1])
    return [(int(a), int(b)) for a, b in zip(edges[::2], edges[1::2])]


def _gutters(mean: np.ndarray, dev: np.ndarray, s: Dict[str, Any]) -> List[tuple[int, int]]:
    flat = dev < s["flat_threshold"]
    steady = np.ones_like(flat)
    steady[1:] = np.abs(np.diff(mean)) < 1.5
    out = []
    for a, b in _runs(flat & steady):
        if b - a < s["gutter_min"]:
            continue
        seg = mean[a:b]
        if float(seg.max() - seg.min()) > s["gutter_drift"]:
            continue  # a gradient (sky, fog), not a gutter
        out.append((a, b))
    return out


def _split_long(a: int, b: int, dev: np.ndarray, norm_w: int) -> List[tuple[int, int]]:
    """Cut a gutterless stretch taller than MAX_PANEL_RATIO at its quietest rows."""
    limit = int(MAX_PANEL_RATIO * norm_w)
    pieces = []
    while b - a > limit:
        target = a + int(limit * 0.8)
        lo, hi = max(a + norm_w, target - norm_w // 2), min(b - norm_w, target + norm_w // 2)
        cut = target if hi <= lo else lo + int(np.argmin(dev[lo:hi]))
        pieces.append((a, cut))
        a = cut
    pieces.append((a, b))
    return pieces


def _cleanup(cands: List[List[int]], s: Dict[str, Any]) -> List[List[int]]:
    cands = [c for c in cands if c[1] - c[0] >= s["drop_below"]]
    changed = True
    while changed and len(cands) > 1:
        changed = False
        for i, (a, b) in enumerate(cands):
            if b - a >= s["merge_below"]:
                continue
            gap_prev = a - cands[i - 1][1] if i > 0 else float("inf")
            gap_next = cands[i + 1][0] - b if i + 1 < len(cands) else float("inf")
            j = i - 1 if gap_prev <= gap_next else i + 1
            lo, hi = sorted((i, j))
            cands[lo] = [min(cands[lo][0], cands[hi][0]), max(cands[lo][1], cands[hi][1])]
            del cands[hi]
            changed = True
            break
    return cands


def _segments(y0: float, y1: float, pages: List[_Page]) -> List[Dict[str, Any]]:
    """Map a global [y0, y1) onto per-page crops in original pixel coordinates."""
    out = []
    for p in pages:
        a, b = max(y0, p.offset), min(y1, p.offset + p.norm_h)
        if b - a < 1:
            continue
        scale = p.h / p.norm_h
        out.append({"page": p.index, "file": p.path.name,
                    "y0": int(round((a - p.offset) * scale)), "y1": int(round((b - p.offset) * scale)),
                    "page_w": p.w})
    return out


def _trim_x(img: Image.Image, norm_w: int) -> tuple[float, float]:
    """Trim uniform side margins. Returns x0, x1 in normalised units."""
    g = np.asarray(img.convert("L").resize((ANALYSIS_WIDTH, max(8, min(img.height, 600)))), dtype=np.float32)
    edge = float(np.median(np.concatenate([g[:, :3].ravel(), g[:, -3:].ravel()])))
    margin_col = (np.abs(g.mean(axis=0) - edge) < 6) & (g.std(axis=0) < 5)
    keep = np.flatnonzero(~margin_col)
    if keep.size == 0:
        return 0.0, float(norm_w)
    # Plain floats: numpy scalars leak into panels.json and json can't serialise them.
    x0 = float(keep[0]) / ANALYSIS_WIDTH * norm_w
    x1 = float(keep[-1] + 1) / ANALYSIS_WIDTH * norm_w
    if x1 - x0 < 0.35 * norm_w:
        return 0.0, float(norm_w)
    pad = 0.01 * norm_w
    return round(max(0.0, x0 - pad), 1), round(min(float(norm_w), x1 + pad), 1)


def trim_edge_rows(img: Image.Image, white: int = WHITE_THRESHOLD, black: int = BLACK_THRESHOLD) -> tuple[int, int]:
    """(rows to cut from the top, rows to cut from the bottom) so the picture starts
    and ends flush against artwork. A row is margin when at least EDGE_ROW_SHARE of
    its pixels are white (> white) or near-black (< black). Only the two outer edges
    are scanned; a white band inside the art is never touched. (0, 0) when nothing
    would be left."""
    g = np.asarray(img.convert("L"), dtype=np.uint8)
    if g.size == 0:
        return 0, 0
    margin = ((g > white).mean(axis=1) >= EDGE_ROW_SHARE) | ((g < black).mean(axis=1) >= EDGE_ROW_SHARE)
    n = len(margin)
    top = 0
    while top < n and margin[top]:
        top += 1
    if top >= n:
        return 0, 0                      # an all-margin panel: leave it for the text-only check
    bottom = 0
    while bottom < n - top and margin[n - 1 - bottom]:
        bottom += 1
    return top, bottom


def detect(pages_paths: List[Path], cfg: Dict[str, Any] | None = None) -> tuple[List[Panel], int]:
    """Find panels across a chapter. Returns (panels, normalised_width)."""
    s = _settings(cfg)
    pages: List[_Page] = []
    for i, p in enumerate(pages_paths, start=1):
        try:
            with Image.open(p) as img:
                w, h = img.size
        except OSError as exc:
            raise UserError(f"Can't read page {p.name}: {exc}") from None
        if s["skip_landscape"] and w > h and _is_flat_banner(p):
            continue  # landscape credit banners, not detailed story spreads
        pages.append(_Page(i, p, w, h))
    if not pages:
        raise UserError("No usable page images (all were landscape credit pages, or none found).")

    widths = [p.w for p in pages]
    norm_w = max(set(widths), key=widths.count)

    means, devs = [], []
    offset = 0.0
    for p in pages:
        mean, dev = _row_stats(p.path, norm_w)
        p.offset, p.norm_h = offset, float(len(mean))
        offset += len(mean)
        means.append(mean)
        devs.append(dev)
    mean, dev = np.concatenate(means), np.concatenate(devs)
    total = len(mean)

    # Content = everything between gutters.
    cands: List[List[int]] = []
    cursor = 0
    for a, b in _gutters(mean, dev, s) + [(total, total)]:
        if a > cursor:
            for piece in _split_long(cursor, a, dev, norm_w):
                cands.append(list(piece))
        cursor = b
    cands = _cleanup(cands, s)

    panels: List[Panel] = []
    per_page: Dict[int, int] = {}
    for idx, (a, b) in enumerate(cands, start=1):
        segs = _segments(a, b, pages)
        if not segs:
            continue
        start_page = segs[0]["page"]
        per_page[start_page] = per_page.get(start_page, 0) + 1
        panel = Panel(id=f"P{start_page:03d}-{per_page[start_page]:02d}", index=idx,
                      y0=float(a), y1=float(b), x0=0.0, x1=float(norm_w), segments=segs)
        try:
            strip = _stitch(panel, {p.path.name: p.path for p in pages})
            if s["tighten_edges"]:
                cut_top, cut_bottom = trim_edge_rows(strip, s["white_threshold"], s["black_threshold"])
                scale = norm_w / max(1, strip.width)
                kept = (b - a) - (cut_top + cut_bottom) * scale
                if (cut_top or cut_bottom) and kept >= max(s["drop_below"], 0.3 * (b - a)):
                    panel.y0, panel.y1 = float(a + cut_top * scale), float(b - cut_bottom * scale)
                    panel.segments = _segments(panel.y0, panel.y1, pages) or panel.segments
                    strip = strip.crop((0, cut_top, strip.width, strip.height - cut_bottom))
            x0, x1 = _trim_x(strip, norm_w)
        except OSError:
            x0, x1 = 0.0, float(norm_w)
        panel.x0, panel.x1 = x0, x1
        panel.tall = bool(panel.height / max(1.0, panel.width) > TALL_RATIO)
        panels.append(panel)
    for i, panel in enumerate(panels, start=1):
        panel.index = i
    return panels, norm_w


# ------------------------------------------------------------------ images


@lru_cache(maxsize=2)
def _load_page(path: str, mtime: float) -> Image.Image:
    # Panels are processed in page order, so the last two decoded pages cover
    # almost every crop. Decoding a 16000px PNG per panel would dominate runtime.
    with Image.open(path) as img:
        return img.convert("RGB")


def _stitch(panel: Panel, files: Dict[str, Path], max_side: int | None = None) -> Image.Image:
    """The panel's full-width strip, stitched across pages, at original resolution."""
    parts = []
    for seg in panel.segments:
        path = files.get(seg["file"])
        if path is None:
            raise OSError(f"page {seg['file']} is missing")
        page = _load_page(str(path), path.stat().st_mtime)
        parts.append(page.crop((0, seg["y0"], page.width, seg["y1"])))
    width = parts[0].width
    parts = [p if p.width == width else p.resize((width, max(1, round(p.height * width / p.width)))) for p in parts]
    out = Image.new("RGB", (width, sum(p.height for p in parts)))
    y = 0
    for p in parts:
        out.paste(p, (0, y))
        y += p.height
    if max_side and max(out.size) > max_side:
        out.thumbnail((max_side, max_side), Image.LANCZOS)
    return out


def _clean_cache_dir(root: Path, chapter: str) -> Path:
    return work_root(root) / "video" / f"ch{require_chapter(chapter)}" / "panels_clean"


def _clean_cache_key(fingerprint: str, panel: Panel, norm_w: int) -> str:
    geo = {"id": panel.id, "x0": panel.x0, "x1": panel.x1, "y0": panel.y0, "y1": panel.y1,
           "segments": [dict(s) for s in panel.segments]}
    raw = json.dumps({"fp": fingerprint, "nw": norm_w, "panel": geo}, sort_keys=True, default=str)
    return hashlib.sha256(raw.encode()).hexdigest()[:20]


def panel_image(root: Path, chapter: str, panel: Dict[str, Any] | Panel, norm_w: int | None = None,
                clean: bool = False, fingerprint: str | None = None) -> Image.Image:
    """Crop one panel from the kept pages, side margins trimmed. clean=True also
    paints out its speech bubbles: that is the picture the video shows."""
    if isinstance(panel, dict):
        panel = Panel(**{k: panel[k] for k in ("id", "index", "y0", "y1", "x0", "x1", "segments")})
    # Cleaned-panel disk cache: stitching + bubble-cleaning (which itself runs
    # _panel_maps) is paid once per panel per fingerprint, not once per caller
    # (verify, designer, storyboard, render workers). The sidecar preserves
    # `painted_boxes`, the attribute _panel_maps reads to steer crops away
    # from painted patches -- a bare PNG would lose it.
    cache_key = cache_png = cache_sidecar = None
    if clean and fingerprint and norm_w is not None:
        cache_key = _clean_cache_key(fingerprint, panel, norm_w)
        cache_dir = _clean_cache_dir(root, chapter)
        cache_png = cache_dir / f"{panel.id}_{cache_key}.png"
        cache_sidecar = cache_dir / f"{panel.id}_{cache_key}.painted.json"
        if cache_png.exists() and cache_sidecar.exists():
            try:
                img = Image.open(cache_png).convert("RGB")
                img.painted_boxes = json.loads(cache_sidecar.read_text(encoding="utf-8"))
                return img
            except (OSError, ValueError):
                pass
    files = {p.name: p for p in list_pages(root, chapter)}
    strip = _stitch(panel, files)
    nw = norm_w or panel.segments[0].get("page_w") or strip.width
    scale = strip.width / nw
    x0, x1 = int(panel.x0 * scale), int(round(panel.x1 * scale))
    if x1 - x0 >= 8:
        strip = strip.crop((x0, 0, min(strip.width, x1), strip.height))
    if clean:
        from .crops import clean_bubbles

        strip = clean_bubbles(strip)
        if cache_key is not None:
            try:
                atomic_save_image(strip, cache_png)
                atomic_write_text(cache_sidecar,
                                  json.dumps(list(getattr(strip, "painted_boxes", []))))
            except OSError:
                pass
    return strip


# ------------------------------------------------------------------ descriptions from tile notes


def _attach_notes(panels: List[Panel], root: Path, chapter: str, pages_meta: Dict[int, tuple[float, float]],
                  norm_w: int) -> None:
    path = media_dir(root, chapter) / "tiles.json"
    try:
        tiles = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return
    for panel in panels:
        scenes, texts = [], []
        for t in tiles:
            meta = pages_meta.get(int(t.get("page", 0)))
            if not meta or not t.get("width"):
                continue
            offset, _ = meta
            f = norm_w / float(t["width"])
            ty0, ty1 = offset + t["top"] * f, offset + t["bottom"] * f
            overlap = min(ty1, panel.y1) - max(ty0, panel.y0)
            if overlap <= 0 or overlap < 0.3 * min(ty1 - ty0, panel.height):
                continue
            if t.get("scene") and t["scene"] not in scenes:
                scenes.append(t["scene"])
            if t.get("text") and t["text"] not in texts:
                texts.append(t["text"])
        panel.scene = " ".join(scenes[:2])[:300]
        panel.text = " / ".join(texts)[:400]


# ------------------------------------------------------------------ build / load


def _fingerprint(pages: List[Path], s: Dict[str, Any]) -> str:
    # The version is in the hash so panels.json files from older releases
    # (missing fields like "art") are re-detected instead of half-used.
    # Edge tightening settings join the hash only when changed from their defaults, so
    # panels.json files made before they existed are not silently re-detected (which
    # would shift framing under recordings that already exist). `video panels --force`
    # re-detects with tightening.
    defaults = {"tighten_edges": True, "white_threshold": WHITE_THRESHOLD, "black_threshold": BLACK_THRESHOLD}
    keyed = {k: v for k, v in s.items() if defaults.get(k, object()) != v}
    h = hashlib.sha256(json.dumps({"v": PANELS_VERSION, **keyed}, sort_keys=True).encode())
    for p in pages:
        st = p.stat()
        try:
            with p.open("rb") as fh:
                head = fh.read(65536)
            h.update(f"{p.name}:{st.st_size}:".encode() + hashlib.sha256(head).digest())
        except OSError:
            h.update(f"{p.name}:{st.st_size}".encode())
    return h.hexdigest()[:16]


def panels_path(root: Path, chapter: str) -> Path:
    return media_dir(root, chapter) / "panels.json"


def load_panels(root: Path, chapter: str) -> Dict[str, Any] | None:
    try:
        data = json.loads(panels_path(root, chapter).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) and data.get("panels") else None


def _pages_of(panel) -> set:
    segs = panel["segments"] if isinstance(panel, dict) else panel.segments
    return {int(seg["page"]) for seg in segs}


def _edge_pages(panels) -> set:
    """The first page and the last two: where credits and promo pages sit."""
    pages = sorted({pg for p in panels for pg in _pages_of(p)})
    return set(pages[:1] + pages[-2:]) if pages else set()


def build_panels(root: Path, chapter: str, cfg: Dict[str, Any] | None = None, *, force: bool = False,
                 log=print) -> Dict[str, Any]:
    """Detect panels (if needed) and write panels.json plus contact sheets."""
    chapter = require_chapter(chapter)
    pages = list_pages(root, chapter)
    if not pages:
        raise UserError(f"No page images for chapter {chapter}.")
    s = _settings(cfg)
    fp = _fingerprint(pages, s)
    existing = load_panels(root, chapter)
    if existing and existing.get("fingerprint") == fp and not force:
        # Notes may have arrived since (chapter transcribed after panels were made).
        return existing

    panels, norm_w = detect(pages, cfg)
    offsets: Dict[int, tuple[float, float]] = {}
    off = 0.0
    for i, p in enumerate(pages, start=1):
        with Image.open(p) as img:
            w, h = img.size
        if s["skip_landscape"] and w > h:
            continue
        nh = round(h * norm_w / w)
        offsets[i] = (off, nh)
        off += nh
    _attach_notes(panels, root, chapter, offsets, norm_w)

    from .crops import FIGURE_MIN, art_score, crop_art, figure_share, is_text_only, looks_like_credits

    rows = []
    edge_pages = _edge_pages(panels)
    for p in panels:
        row = {**asdict(p), "height": round(p.height, 1), "width": round(p.width, 1)}
        try:
            img = panel_image(root, chapter, row, norm_w)
            row["art"], row["crop_art"] = art_score(img), crop_art(img)
            row["figure"] = figure_share(img)
        except OSError:
            row["art"], row["crop_art"], row["figure"] = 1.0, 1.0, 1.0
        # Credits/promo pages (scanlator names, "Read at ...") are never shown.
        row["credits"] = bool(edge_pages & _pages_of(row)) and looks_like_credits(row.get("text") or "")
        # A panel with a character in it is art even when it has little
        # texture (a pale portrait, a plain background): only lettering, caption
        # boxes and blank pages are text-only.
        has_notes = bool((row.get("scene") or "").strip() or (row.get("text") or "").strip())
        row["text_only"] = row["credits"] or (
            is_text_only(row["art"], row["crop_art"])
            and row["figure"] < FIGURE_MIN
            and not has_notes
        )
        rows.append(row)

    data = {
        "version": PANELS_VERSION,
        "chapter": chapter,
        "fingerprint": fp,
        "norm_width": norm_w,
        "tight": bool(s["tighten_edges"]),
        "pages": [p.name for p in pages],
        "panels": rows,
    }
    atomic_write_text(panels_path(root, chapter), json.dumps(data, indent=1, ensure_ascii=False))
    write_sheets(root, chapter, data, log=log)
    log(f"  chapter {chapter}: {len(panels)} panels from {len(pages)} pages")
    return data


def sheet_paths(root: Path, chapter: str) -> List[Path]:
    return sorted(media_dir(root, chapter).glob("panels_sheet_*.png"))


def _font(size: int):
    try:
        return ImageFont.load_default(size=size)
    except TypeError:  # Pillow < 10.1
        return ImageFont.load_default()


def write_sheets(root: Path, chapter: str, data: Dict[str, Any], log=print) -> List[Path]:
    """Contact sheets: labelled thumbnails, 12 per image, for checking the split."""
    for old in sheet_paths(root, chapter):
        old.unlink()
    panels = data["panels"]
    cols, cell_w, cell_h, label_h = 4, 380, 560, 34
    font = _font(26)
    out = []
    for n, start in enumerate(range(0, len(panels), SHEET_PER_PAGE), start=1):
        chunk = panels[start:start + SHEET_PER_PAGE]
        rows = (len(chunk) + cols - 1) // cols
        sheet = Image.new("RGB", (cols * cell_w, rows * (cell_h + label_h)), (32, 32, 32))
        draw = ImageDraw.Draw(sheet)
        for i, p in enumerate(chunk):
            cx, cy = (i % cols) * cell_w, (i // cols) * (cell_h + label_h)
            try:
                thumb = panel_image(root, chapter, p, data["norm_width"])
                thumb.thumbnail((cell_w - 16, cell_h - 12), Image.LANCZOS)
                sheet.paste(thumb, (cx + (cell_w - thumb.width) // 2, cy + label_h + (cell_h - thumb.height) // 2))
            except OSError:
                pass
            tag = f"{p['id']}{'  (tall)' if p.get('tall') else ''}"
            draw.text((cx + 10, cy + 4), tag, fill=(255, 214, 90), font=font)
            if p.get("text_only"):
                note = "CREDITS - not used" if p.get("credits") else "TEXT - don't use"
                draw.text((cx + cell_w - 12 - draw.textlength(note, font=font), cy + 4), note,
                          fill=(240, 90, 90), font=font)
        path = media_dir(root, chapter) / f"panels_sheet_{n:02d}.png"
        atomic_save_image(sheet, path)
        out.append(path)
    return out
