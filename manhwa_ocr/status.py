"""Session state, computed from what is on disk.

One source of truth for "where is this series up to?", shared by the `next` and
`status` commands and the dashboard, so they can never disagree. Every
instruction it produces names its session explicitly, so an agent following
them stays on the right series even if the active session changes underneath.

Nothing here raises on a damaged file: a broken memory or link file is reported
as a problem to fix, because this runs on every dashboard refresh.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List

from .config import find_config, load_config
from .memory import SeriesMemory
from .script import RecapBuilder, _count_words
from .sessions import Session
from .util import UserError, chapter_key, chapter_spec
from .workspace import (
    STAGE_DONE,
    chapter_stage,
    dir_size_mb,
    stage_instruction,
    tile_progress,
    work_root,
    chapter_work_dir,
)

STAGE_LABELS = {
    "done": "Transcribed",
    "prepare": "Not started",
    "failed": "Download failed",
    "read_tiles": "Reading tiles",
    "assemble": "Tiles read",
    "answer_assembly": "Assembling",
    "finalize": "Ready to finalize",
}


def _cmd(args: str, sess: Session) -> str:
    return f'python -m manhwa_ocr {args} --session "{sess.slug}"'


def builder_for(sess: Session) -> RecapBuilder:
    cfg = load_config(find_config(), series__workdir=str(sess.path), series__name=sess.name)
    return RecapBuilder(cfg, log=lambda *_: None, session=sess.slug)


def _memory(sess: Session) -> tuple[SeriesMemory, str | None]:
    try:
        return SeriesMemory.load(sess.memory_path, sess.name), None
    except UserError as exc:
        return SeriesMemory(series=sess.name), str(exc)


def next_steps(sess: Session) -> List[str]:
    """The single next action for this session, as printable lines."""
    _, mem_error = _memory(sess)
    if mem_error:
        return ["The series memory file is damaged, so nothing can run until it's fixed:", f"  {mem_error}"]

    try:
        recap_task = builder_for(sess).pending_task()
    except UserError:
        recap_task = None
    if recap_task:
        task = (work_root(sess.path) / "recap" / "TASK.md").as_posix()
        return [
            f"A recap task for chapter(s) {', '.join(recap_task)} is waiting.",
            f"  Read:   {task}",
            "  Write:  .work/recap/parts.json",
            f"  Then:   {_cmd('recap --finalize', sess)}",
            f"  (or discard it: {_cmd('recap --cancel', sess)})",
        ]

    links, problems = sess.parse_links()
    if not links:
        lines = [f"Series '{sess.name}' has no chapter links yet.", "Paste them into:", f"  {sess.links_path}"]
        return lines + [f"  ! {p}" for p in problems[:5]]

    malformed = [p for p in problems if "malformed" in p]
    for chapter, _ in links:
        stage = chapter_stage(sess.path, chapter)
        if stage != STAGE_DONE:
            remaining = sum(1 for c, _ in links if chapter_stage(sess.path, c) != STAGE_DONE)
            lines = stage_instruction(stage, chapter, sess.path, sess.slug).splitlines()
            lines += ["", f"({remaining} chapter(s) remaining)"]
            if malformed:
                lines += ["", "Fix these lines in chapters.txt:"] + [f"  ! {p}" for p in malformed[:5]]
            return lines

    lines = [f"STEP 1 done: all {len(links)} chapters are transcribed."]
    if malformed:
        lines += ["", "But some lines in chapters.txt are broken and were skipped:"]
        lines += [f"  ! {p}" for p in malformed[:5]]
    return lines + after_transcription(sess, [c for c, _ in links])


def after_transcription(sess: Session, chapters: List[str]) -> List[str]:
    """Steps 2-4 of the workflow: recaps (agent), audio (user), videos (agent)."""
    recapped = set(sess.recaps())
    no_recap = [c for c in chapters if c not in recapped]
    need_audio: List[str] = []
    need_video: List[str] = []
    try:
        from .video.build import chapter_status

        for c in chapters:
            if c not in recapped:
                continue
            st = chapter_status(sess.path, c)
            if not st["audio"]:
                need_audio.append(c)
            elif not (st["video"] and st["video_up_to_date"]):
                need_video.append(c)
    except Exception:  # noqa: BLE001 - video is optional; never break `next`
        need_audio, need_video = [], []

    lines: List[str] = []
    if no_recap:
        lines += ["", f"STEP 2 (agent) - write recaps for chapter(s) {chapter_spec(no_recap)}:",
                  f"  Run:  {_cmd(f'recap --chapters {chapter_spec(no_recap)} --prepare', sess)}",
                  "  Then follow the TASK.md it prints."]
    if need_video:
        spec = chapter_spec(need_video)
        lines += ["", f"STEP 4 (agent) - narration audio is ready, make the video for chapter(s) {spec}:",
                  f"  1. Check the pictures:  {_cmd(f'video pair --chapters {spec}', sess)}",
                  "     Look at every pairing sheet, fix .work/video/chN/pairs.json, then run it again with --apply",
                  f"  2. Build:  {_cmd(f'video build --chapters {spec}', sess)}",
                  f"  3. Check:  {_cmd('video verify --chapter N', sess)}",
                  "     Open the storyboards and .work/video/chN/NARRATION.md. Fix framing only in video/chapter_N.fixes.json,",
                  "     pictures only in pairs.json. Never edit the plan by hand.",
                  f"  4. Finish: {_cmd('video approve --chapter N', sess)}   then   {_cmd('video render --chapter N', sess)}",
                  "  The full routine and rules: dashboard \"Copy agent prompt: build videos\", or USER_GUIDE.md (Step 5)."]
    if need_audio:
        from .video.cues import tts_path

        first = need_audio[0]
        script = tts_path(sess.path, first)
        if not script.exists():
            script = sess.recap_path(first)
        lines += ["", f"STEP 3 (you, not the agent) - record narration for chapter(s) {chapter_spec(need_audio)}:",
                  f"  Script to read or paste into TTS:  {script}",
                  f"  Save the recording as:            {sess.path / 'audio' / f'chapter_{first}.mp3'}",
                  "  (same for the other chapters; .wav and .m4a work too, or upload on the dashboard's Video tab)",
                  "  Skip this step if you only want scripts, not videos."]
        if not no_recap and not need_video:
            lines += ["", "AGENT: nothing left for you to do. Stop here and tell the user which chapters need audio,"
                          " and where to save it. Run `next` again once they have added it."]
    if not (no_recap or need_audio or need_video):
        everything = chapter_spec(chapters)
        lines += ["", "STEP 4 done: every chapter has a recap and an up-to-date video.",
                  f"  One long video:   {_cmd(f'video compile --chapters {everything}', sess)}",
                  f"  One long script:  {_cmd(f'compile --chapters {everything}', sess)}",
                  f"  A Short:          {_cmd('video clip --chapter N --from 30 --to 90 --format 9:16', sess)}"]
    return lines



def _words_in(path: Path) -> int:
    try:
        return _count_words(path.read_text(encoding="utf-8"))
    except OSError:
        return 0


def session_summary(sess: Session, active_slug: str | None) -> Dict[str, Any]:
    links = sess.read_links()
    mem, _ = _memory(sess)
    return {
        "slug": sess.slug,
        "name": sess.name,
        "active": sess.slug == active_slug,
        "last_used": sess.last_used,
        "links": len(links),
        "transcribed": len(sess.chapters()),
        "recapped": len(sess.recaps()),
        "characters": len(mem.characters),
    }


def session_detail(sess: Session, active_slug: str | None) -> Dict[str, Any]:
    builder = builder_for(sess)
    links_list, link_problems = sess.parse_links()
    links = dict(links_list)
    transcribed = set(sess.chapters())
    recapped = set(sess.recaps())

    # Chapters can exist without a link (imported transcripts), so union them.
    labels = sorted(set(links) | transcribed | recapped, key=chapter_key)

    chapters = []
    for c in labels:
        stage = chapter_stage(sess.path, c)
        recap_meta = builder._read_meta(sess.recap_path(c)) if c in recapped else {}
        got, total, _ = tile_progress(chapter_work_dir(sess.path, c)) if stage == "read_tiles" else (0, 0, True)
        chapters.append(
            {
                "chapter": c,
                "url": links.get(c, ""),
                "stage": stage,
                "stage_label": (f"Reading tiles {got}/{total}" if stage == "read_tiles" and total
                                else STAGE_LABELS.get(stage, stage)),
                "transcribed": c in transcribed,
                "recapped": c in recapped,
                "transcript_words": _words_in(sess.path / f"chapter_{c}.md") if c in transcribed else 0,
                "recap_words": recap_meta.get("words") or (_words_in(sess.recap_path(c)) if c in recapped else 0),
                "recap_hook": recap_meta.get("hook", ""),
            }
        )

    compilations = []
    for p in sess.compilations():
        try:
            compilations.append({"name": p.stem, "words": _words_in(p), "modified": p.stat().st_mtime})
        except OSError:
            continue

    mem, mem_error = _memory(sess)
    problems = list(link_problems)
    if mem_error:
        problems.insert(0, mem_error)

    video = []
    try:
        from .video.build import chapter_status

        for c in labels:
            if c in transcribed:
                video.append(chapter_status(sess.path, c, probe=False))
    except Exception:  # noqa: BLE001 - video panel must never break the dashboard
        video = []
    return {
        "video": video,
        **session_summary(sess, active_slug),
        "path": str(sess.path),
        "links_path": str(sess.links_path),
        "next": next_steps(sess),
        "problems": problems[:20],
        "recap_task": builder.pending_task(),
        "chapters": chapters,
        "compilations": compilations,
        "scratch_mb": round(dir_size_mb(work_root(sess.path)), 1),
        "memory": {
            "characters": list(mem.characters.values()),
            "world_facts": mem.world_facts,
            "plot_threads": mem.plot_threads,
            "glossary": mem.glossary,
        },
    }
