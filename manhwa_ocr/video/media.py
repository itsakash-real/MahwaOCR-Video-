"""Where video inputs and outputs live inside a series folder.

    <session>/audio/chapter_N.mp3      the narration you record
    <session>/media/chN/pages/         chapter pages, kept after transcription
    <session>/media/chN/panels.json    panel boxes (no duplicate images)
    <session>/video/chapter_N.*        plan, timeline, storyboard, mp4
"""

from __future__ import annotations

import re
import shutil
from pathlib import Path
from typing import Callable, List

from ..util import UserError, atomic_write_text, normalize_label, require_chapter

AUDIO_EXTS = (".mp3", ".wav", ".m4a", ".ogg", ".flac", ".aac", ".opus")
GITIGNORE = "# Large media: kept on disk, kept out of git.\nmedia/\naudio/\nvideo/*.mp4\n.work/\n"


def media_dir(root: Path, chapter: str) -> Path:
    return Path(root) / "media" / f"ch{require_chapter(chapter)}"


def pages_dir(root: Path, chapter: str) -> Path:
    return media_dir(root, chapter) / "pages"


def video_dir(root: Path) -> Path:
    return Path(root) / "video"


def audio_dir(root: Path) -> Path:
    return Path(root) / "audio"


def list_pages(root: Path, chapter: str) -> List[Path]:
    from ..images import list_images

    d = pages_dir(root, chapter)
    return list_images(d) if d.is_dir() else []


def keep_pages(root: Path, chapter: str, work_pages: Path) -> bool:
    """Move a chapter's downloaded pages out of scratch so video can use them."""
    if not work_pages.is_dir() or not any(work_pages.iterdir()):
        return False
    dest = pages_dir(root, chapter)
    if dest.exists():
        shutil.rmtree(dest, ignore_errors=True)
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(work_pages), str(dest))
    return True


def audio_path(root: Path, chapter: str) -> Path | None:
    """audio/chapter_N.<ext>, accepting zero-padded names like chapter_03.mp3."""
    folder = audio_dir(root)
    if not folder.is_dir():
        return None
    want = require_chapter(chapter)
    for p in sorted(folder.iterdir()):
        m = re.fullmatch(r"chapter_(.+?)(\.[A-Za-z0-9]+)", p.name)
        if m and p.suffix.lower() in AUDIO_EXTS and p.stat().st_size > 0:
            if normalize_label(m.group(1)) == want:
                return p
    return None


def save_tile_notes(root: Path, chapter: str, notes: List[dict]) -> None:
    """Keep what each tile showed (position, scene, text) after tiles are deleted.

    Panels for video are found later on the full pages; overlapping these notes
    tells the recap writer what each panel depicts.
    """
    import json

    clean = []
    for n in notes:
        try:
            clean.append({
                "id": str(n["id"]), "page": int(n["page"]), "top": int(n["top"]),
                "bottom": int(n["bottom"]), "width": int(n.get("width") or 0),
                "scene": str(n.get("scene") or "")[:400],
                "text": str(n.get("text") or "")[:600],
            })
        except (KeyError, TypeError, ValueError):
            continue
    if clean:
        atomic_write_text(media_dir(root, chapter) / "tiles.json",
                          json.dumps(clean, indent=1, ensure_ascii=False))


def tile_notes_from(entry: dict) -> tuple[str, str]:
    """(scene, joined text) from one tile result entry, tolerant of sloppy shapes."""
    if not isinstance(entry, dict):
        return "", ""
    texts = []
    for b in entry.get("blocks") or []:
        if isinstance(b, dict) and b.get("text"):
            who = b.get("speaker")
            texts.append(f"{who}: {b['text']}" if who else str(b["text"]))
        elif isinstance(b, str):
            texts.append(b)
    return str(entry.get("scene") or ""), " / ".join(texts)


def ensure_gitignore(root: Path) -> None:
    gi = Path(root) / ".gitignore"
    if not gi.exists():
        atomic_write_text(gi, GITIGNORE)


def ensure_pages(sess, chapter: str, download_cfg: dict, log: Callable[[str], None] = print) -> List[Path]:
    """Pages for a chapter, re-downloading them if they were cleaned up earlier."""
    chapter = require_chapter(chapter)
    pages = list_pages(sess.path, chapter)
    if pages:
        return pages
    url = dict(sess.read_links()).get(chapter)
    if not url:
        raise UserError(
            f"No page images kept for chapter {chapter}, and no link for it in chapters.txt.\n"
            f"Save the chapter's images into {pages_dir(sess.path, chapter)} (any image names; "
            "they're read in natural order), then run the command again."
        )
    from ..images import download_images
    from ..scrapers import discover_pages

    log(f"  chapter {chapter}: page images aren't on disk; downloading them again...")
    urls = discover_pages(url, download_cfg)
    return download_images(urls, pages_dir(sess.path, chapter), referer=url, config=download_cfg, log=log,
                           cache_dir=sess.path / ".cache" / "pages")
