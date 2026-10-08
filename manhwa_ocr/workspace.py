"""Scratch-file layout and cleanup.

Everything a run generates that is not a deliverable lives under `.work/`:

    <session>/
      chapter_1.md          <- deliverable, kept
      recap_script.md       <- deliverable, kept
      chapters.txt          <- your input, kept
      series_memory.json    <- state, kept
      .cache/               <- small JSON, makes re-runs cheap, kept
      .work/                <- scratch, deleted after each chapter finalizes
        ch1/
          pages/  tiles/  TASK.md  manifest.json  tile_results.json
          ASSEMBLE.md  assembled.json  raw_blocks.txt

`pages/` and `tiles/` are by far the largest thing on disk -- a 30-page chapter
is comfortably a few hundred megabytes once sliced. Clearing them after the
transcript is written keeps a session small enough to commit to git.

Re-running a cleaned chapter re-downloads it. Pass `--keep` on finalize, or set
`pipeline.cleanup: false`, when you are iterating and want the tiles to stay.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import List

WORK_DIRNAME = ".work"


def work_root(session_root: Path) -> Path:
    return session_root / WORK_DIRNAME


def chapter_work_dir(session_root: Path, chapter: str) -> Path:
    return work_root(session_root) / f"ch{chapter}"


def dir_size_mb(path: Path) -> float:
    if not path.exists():
        return 0.0
    total = sum(f.stat().st_size for f in path.rglob("*") if f.is_file())
    return total / (1024 * 1024)


def clean_chapter(session_root: Path, chapter: str, *, keep_pages: bool = True) -> float:
    """Delete one chapter's scratch directory. Returns MB reclaimed.

    The original page images are moved to media/chN/pages first: video
    rendering needs them, and re-downloading is slow and not always possible.
    Tiles (the bulk) are deleted.
    """
    target = chapter_work_dir(session_root, chapter)
    if not target.exists():
        return 0.0
    if keep_pages:
        from .video.media import keep_pages as _keep

        _keep(session_root, chapter, target / "pages")
    freed = dir_size_mb(target)
    shutil.rmtree(target, ignore_errors=True)
    try:
        work_root(session_root).rmdir()  # leave no empty .work/ behind
    except OSError:
        pass
    return freed


def clean_all(session_root: Path) -> tuple[int, float]:
    """Delete every chapter's scratch. Returns (chapters_cleaned, MB freed)."""
    root = work_root(session_root)
    if not root.exists():
        return 0, 0.0
    freed = dir_size_mb(root)
    count = len([p for p in root.iterdir() if p.is_dir()])
    shutil.rmtree(root, ignore_errors=True)
    return count, freed


def clean_finished(session_root: Path) -> tuple[int, float]:
    """Delete scratch only for chapters whose transcript exists.

    In-progress chapters keep their scratch: it holds tile reading an agent may
    have spent a long time on and has not been turned into a transcript yet.
    """
    count, freed = 0, 0.0
    for chapter, _mb in list_work(session_root):
        done = session_root / f"chapter_{chapter}.md"
        if done.exists() and done.stat().st_size > 0:
            freed += clean_chapter(session_root, chapter)
            count += 1
    return count, freed


def list_work(session_root: Path) -> List[tuple[str, float]]:
    """[(chapter, MB), ...] for scratch still on disk."""
    root = work_root(session_root)
    if not root.exists():
        return []
    out = []
    for p in sorted(root.iterdir()):
        if p.is_dir() and p.name.startswith("ch"):
            out.append((p.name[2:], dir_size_mb(p)))
    return out


# ---------------------------------------------------------------- state


# Per-chapter pipeline stages, in order. Used by `manhwa-ocr next` so an agent
# can always ask "what now?" instead of tracking position itself.
STAGE_DONE = "done"
STAGE_PREPARE = "prepare"
STAGE_READ_TILES = "read_tiles"
STAGE_ASSEMBLE = "assemble"
STAGE_ANSWER = "answer_assembly"
STAGE_FINALIZE = "finalize"


STAGE_FAILED = "failed"

FAILED_MARKER = "FAILED.txt"


def _cmd(args: str, session: str | None) -> str:
    pin = f' --session "{session}"' if session else ""
    return f"python -m manhwa_ocr {args}{pin}"


def tile_progress(work: Path) -> tuple[int, int, bool]:
    """(tiles_with_results, total_tiles, results_file_parses).

    Never raises: this runs on every `next` and every dashboard refresh.
    """
    from .util import read_agent_json  # local import keeps workspace dependency-light

    try:
        manifest = json.loads((work / "manifest.json").read_text(encoding="utf-8"))
        ids = [t["id"] for t in manifest["tiles"]]
    except (OSError, ValueError, KeyError, TypeError):
        return 0, 0, True
    results_path = work / "tile_results.json"
    if not results_path.exists():
        return 0, len(ids), True
    try:
        data = read_agent_json(results_path, "")
    except Exception:  # noqa: BLE001 - reported as "doesn't parse"
        return 0, len(ids), False
    if isinstance(data, dict):
        lists = [v for v in data.values() if isinstance(v, list)]
        data = lists[0] if len(lists) == 1 else [{"id": k, **v} for k, v in data.items() if isinstance(v, dict)]
    entries = [e for e in data if isinstance(e, dict)] if isinstance(data, list) else []
    got = {str(e.get("id")) for e in entries if e.get("id") is not None}
    if not got and len(entries) == len(ids):
        return len(ids), len(ids), True  # id-less but complete, matched by position
    return len(got & set(ids)), len(ids), True


def chapter_stage(session_root: Path, chapter: str) -> str:
    """Where this chapter is in the agent-mode pipeline."""
    done = session_root / f"chapter_{chapter}.md"
    if done.exists() and done.stat().st_size > 0:
        return STAGE_DONE
    work = chapter_work_dir(session_root, chapter)
    if (work / FAILED_MARKER).exists() and not (work / "TASK.md").exists():
        return STAGE_FAILED
    if not (work / "TASK.md").exists():
        return STAGE_PREPARE
    got, total, parses = tile_progress(work)
    # Partial results are the normal state after an agent restart mid-chapter.
    # Treating them as "read" would finalize a transcript with holes in it.
    if not parses or total == 0 or got < total:
        return STAGE_READ_TILES
    if not (work / "ASSEMBLE.md").exists():
        return STAGE_ASSEMBLE
    if not (work / "assembled.json").exists():
        return STAGE_ANSWER
    return STAGE_FINALIZE


def record_failure(session_root: Path, chapter: str, reason: str) -> None:
    work = chapter_work_dir(session_root, chapter)
    work.mkdir(parents=True, exist_ok=True)
    (work / FAILED_MARKER).write_text(reason.strip() + "\n", encoding="utf-8")


def clear_failure(session_root: Path, chapter: str) -> None:
    marker = chapter_work_dir(session_root, chapter) / FAILED_MARKER
    if marker.exists():
        marker.unlink()


def stage_instruction(
    stage: str, chapter: str, session_root: Path, session: str | None = None
) -> str:
    """Human/agent-readable next action for a chapter."""
    work = chapter_work_dir(session_root, chapter)
    rel = lambda p: str(p).replace("\\", "/")  # noqa: E731

    if stage == STAGE_PREPARE:
        return (
            f"Chapter {chapter} has not been downloaded yet.\n"
            f"  Run:  {_cmd('prepare --next', session)}"
        )
    if stage == STAGE_FAILED:
        try:
            reason = (work / FAILED_MARKER).read_text(encoding="utf-8").strip()
        except OSError:
            reason = "unknown error"
        return (
            f"Chapter {chapter} could not be downloaded automatically:\n"
            f"  {reason.splitlines()[0][:200]}\n"
            f"Ask the user to save that chapter's images into a folder, then run:\n"
            f"  {_cmd(f'prepare --chapter {chapter} --folder <that folder>', session)}\n"
            f"Or retry the download:  {_cmd(f'prepare --chapter {chapter}', session)}"
        )
    if stage == STAGE_READ_TILES:
        got, total, parses = tile_progress(work)
        if not parses:
            state = "tile_results.json exists but is not valid JSON; fix it, keeping what is already there"
        elif got:
            state = f"{got} of {total} tiles read so far; read the remaining ones and add them"
        else:
            state = f"{total} tiles waiting to be read"
        return (
            f"Chapter {chapter}: {state}.\n"
            f"  Tiles and prompt:  {rel(work / 'TASK.md')}\n"
            f"  Write results to:  {rel(work / 'tile_results.json')}"
        )
    if stage == STAGE_ASSEMBLE:
        return (
            f"Chapter {chapter}: all tiles are read.\n"
            f"  Run:  {_cmd(f'assemble --chapter {chapter}', session)}"
        )
    if stage == STAGE_ANSWER:
        return (
            f"Chapter {chapter} is waiting on the assembly step.\n"
            f"  Answer the prompt in: {rel(work / 'ASSEMBLE.md')}\n"
            f"  Write the JSON to:    {rel(work / 'assembled.json')}"
        )
    if stage == STAGE_FINALIZE:
        return (
            f"Chapter {chapter} is assembled and ready to write.\n"
            f"  Run:  {_cmd(f'finalize --chapter {chapter}', session)}"
        )
    return f"Chapter {chapter} is done."
