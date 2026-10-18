"""Storyboard and review task, so an agent (or you) can check a plan before rendering.

The storyboard draws each shot's middle frame with the same renderer the video
uses, labelled with its time, panel id and the sentence being spoken. A vision
agent can look at it and spot "the narration says the sword breaks but the
panel shows a forest", then fix that one shot in the plan.
"""

from __future__ import annotations

from collections import OrderedDict
import textwrap
from pathlib import Path
from typing import Any, Dict, List

from PIL import Image, ImageDraw

from ..util import atomic_save_image, atomic_write_text, require_chapter
from ..workspace import work_root
from .frames import ShotRenderer, format_from
from .media import video_dir
from .panels import _font, load_panels, panel_image

PER_SHEET = 24
COLS = 4


def storyboard_paths(root: Path, chapter: str) -> List[Path]:
    return sorted(video_dir(root).glob(f"chapter_{require_chapter(chapter)}.storyboard_*.png"))


def _clock(t: float) -> str:
    m, s = divmod(max(0.0, t), 60)
    return f"{int(m)}:{s:04.1f}"


def write_storyboard(root: Path, chapter: str, plan: Dict[str, Any], log=print) -> List[Path]:
    chapter = require_chapter(chapter)
    for old in storyboard_paths(root, chapter):
        old.unlink()
    panels_data = load_panels(root, chapter) or {"panels": [], "norm_width": None}
    panels = {p["id"]: p for p in panels_data["panels"]}
    fmt = format_from(plan.get("format", "16:9"), 30, preview=True)
    thumb_w = 400 if not fmt.portrait else 230
    thumb_h = round(thumb_w * fmt.height / fmt.width)
    text_h, pad = 92, 10
    font, small = _font(18), _font(15)
    shots = plan["shots"]
    cache: "OrderedDict[str, Image.Image]" = OrderedDict()
    out = []
    for n, start in enumerate(range(0, len(shots), PER_SHEET), start=1):
        chunk = shots[start:start + PER_SHEET]
        rows = (len(chunk) + COLS - 1) // COLS
        cell_w, cell_h = thumb_w + 2 * pad, thumb_h + text_h + 2 * pad
        sheet = Image.new("RGB", (COLS * cell_w, rows * cell_h), (24, 24, 24))
        draw = ImageDraw.Draw(sheet)
        for i, shot in enumerate(chunk):
            x, y = (i % COLS) * cell_w + pad, (i // COLS) * cell_h + pad
            ids = [c.get("panel") for c in shot.get("cards") or []]
            if shot.get("bg_panel"):
                ids.append(shot["bg_panel"])
            missing = [p for p in ids if p not in panels]
            if not missing and ids:
                images = {}
                for pid in dict.fromkeys(ids):
                    if pid in cache:
                        cache.move_to_end(pid)
                    else:
                        cache[pid] = panel_image(root, chapter, panels[pid], panels_data["norm_width"], clean=True)
                        # LRU: evict one entry, never wipe the whole cache.
                        while len(cache) > 32:
                            cache.popitem(last=False)
                    images[pid] = cache[pid]
                # 85% through: entrances done and a pair's second card has arrived.
                when = float(shot["start"]) + 0.85 * (float(shot["end"]) - float(shot["start"]))
                frame = ShotRenderer(shot, images, fmt, {"punch": False, "shake": False, "flash": False}).frame(when)
                sheet.paste(frame.resize((thumb_w, thumb_h)), (x, y))
                wordy = [pid for pid in dict.fromkeys(ids) if panels[pid].get("text_only")]
                if wordy:
                    # Hand-picked bubble/lettering/credits panels: flag them for the reviewer.
                    draw.rectangle((x, y, x + thumb_w - 1, y + thumb_h - 1), outline=(230, 60, 60), width=4)
                    draw.text((x + 8, y + 6), f"TEXT PANEL {', '.join(wordy)}: pick a drawn one",
                              fill=(255, 90, 90), font=font, stroke_width=2, stroke_fill=(0, 0, 0))
            else:
                draw.rectangle((x, y, x + thumb_w, y + thumb_h), outline=(200, 60, 60), width=3)
                draw.text((x + 8, y + 8), f"MISSING PANEL {', '.join(map(str, missing))}", fill=(230, 80, 80), font=font)
            fx = ",".join(e["type"] for e in shot.get("effects") or [])
            head = f"{shot.get('id')}  {_clock(float(shot['start']))}-{_clock(float(shot['end']))}  {' + '.join(map(str, ids[:2]))}"
            draw.text((x, y + thumb_h + 4), head, fill=(255, 214, 90), font=font)
            enters = "/".join(c.get("enter", "cut") for c in shot.get("cards") or [])
            style = f"{shot.get('layout')} · {shot.get('background')} · {enters}{('  +' + fx) if fx else ''}"
            draw.text((x, y + thumb_h + 26), style, fill=(150, 190, 255), font=small)
            said = textwrap.shorten(shot.get("sentence") or "(no sentence: even timing)", 120, placeholder="…")
            for k, line in enumerate(textwrap.wrap(said, 48)[:2]):
                draw.text((x, y + thumb_h + 46 + 18 * k), line, fill=(220, 220, 220), font=small)
        path = video_dir(root) / f"chapter_{chapter}.storyboard_{n:02d}.png"
        atomic_save_image(sheet, path)
        out.append(path)
    log(f"  storyboard: {len(out)} image(s), {len(shots)} shots")
    return out


def write_review_task(root: Path, chapter: str, plan: Dict[str, Any], session: str | None) -> Path:
    pin = f' --session "{session}"' if session else ""
    boards = "\n".join(f"- `{p.relative_to(root).as_posix()}`" for p in storyboard_paths(root, chapter))
    sheets = "\n".join(f"- `{p.relative_to(root).as_posix()}`"
                       for p in sorted((root / "media" / f"ch{chapter}").glob("panels_sheet_*.png")))
    sync_note = ("Shots follow the narration sentence by sentence."
                 if plan.get("sync") == "synced" else
                 "This plan uses even timing (no sentence sync), so only check for broken or unreadable shots.")
    from .. import agent_prompts

    brief = agent_prompts.render("review", series=session or "this series", session=session or "<session>", N=chapter)
    doc = f"""# Video review — chapter {chapter}

{sync_note}

The brief below is the canonical review prompt (manhwa_ocr/agent_prompts.py). Follow it exactly.

## Look at

Storyboard (each shot's middle frame, its time, panel id, and the words being spoken):
{boards}

Panel contact sheets (every available panel with its id):
{sheets}

## Brief

{brief}
"""
    path = work_root(root) / "video" / f"ch{chapter}" / "REVIEW.md"
    atomic_write_text(path, doc)
    return path
