"""Command-line interface.

Every command that reads or writes series data runs inside a session. Expected
problems raise UserError and are printed as one actionable message, never a
traceback -- the driving agent needs to know what to fix, not where in the code
it broke. Set MANHWA_DEBUG=1 to see full tracebacks.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
import traceback
from datetime import datetime
from pathlib import Path

from .config import find_config, load_config, write_example_config
from .memory import SeriesMemory
from .util import UserError, expand_chapters, normalize_label, require_chapter, sort_chapters


_OUTPUT_GONE = False


def _log(msg: str = "") -> None:
    """Print, but never let output kill a command.

    If whatever is reading stdout goes away (an agent's pipe closing, `| head`),
    Windows raises OSError(EINVAL) rather than BrokenPipeError. Without this, a
    command could die halfway through its work just because nobody was
    listening any more.
    """
    global _OUTPUT_GONE
    if _OUTPUT_GONE:
        return
    try:
        print(msg, flush=True)
    except (BrokenPipeError, OSError):
        _OUTPUT_GONE = True
        try:
            # Point stdout at the null device so the interpreter's own flush at
            # exit doesn't raise again.
            devnull = os.open(os.devnull, os.O_WRONLY)
            os.dup2(devnull, sys.stdout.fileno())
        except (OSError, ValueError, AttributeError):
            pass


def _setup_console() -> None:
    # Windows consoles default to a legacy code page; printing a Korean series
    # name or an em dash would otherwise crash mid-run.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError, OSError):
            pass


def _store(args):
    from .sessions import SessionStore

    return SessionStore(getattr(args, "sessions_root", None))


def _get_session(store, name: str):
    try:
        return store.get(name)
    except KeyError as exc:
        raise UserError(str(exc).strip("'\"")) from None
    except ValueError as exc:
        raise UserError(str(exc)) from None


def _session(args, *, required: bool = True):
    """The session a command should act on: --session, else the active one."""
    store = _store(args)
    name = getattr(args, "session", None)
    sess = _get_session(store, name) if name else store.active()
    if sess is None and required:
        raise UserError(
            "No series selected.\n"
            '  Start one:   python -m manhwa_ocr start "Series Name"\n'
            "  Or pick one: python -m manhwa_ocr session list   then   session use <name>"
        )
    return sess


def _load_cfg(args, **extra):
    """Config for a command. Output always goes to a session unless --workdir is given."""
    workdir = getattr(args, "workdir", None)
    series = getattr(args, "series", None)
    sess = None
    if not workdir:
        sess = _session(args)
        workdir, series = str(sess.path), series or sess.name
    cfg = load_config(find_config(getattr(args, "config", None)), series__name=series,
                      series__workdir=workdir, **extra)
    return cfg, sess


def _build_pipeline(args):
    from .pipeline import Pipeline

    cfg, sess = _load_cfg(
        args,
        provider__kind=getattr(args, "provider", None),
        provider__vision_model=getattr(args, "model", None),
        provider__base_url=getattr(args, "base_url", None),
        provider__api_key_env=getattr(args, "api_key_env", None),
    )
    if sess:
        _log(f"Series:   {sess.name}  ({sess.path})")
    return Pipeline(cfg, log=_log), sess


def _build_flow(args):
    from .agentflow import AgentFlow

    cfg, sess = _load_cfg(args)
    if sess:
        _log(f"Series:   {sess.name}")
    return AgentFlow(cfg, log=_log, session=sess.slug if sess else None), sess


def _pin(sess) -> str:
    return f' --session "{sess.slug}"' if sess else ""


# ---------------------------------------------------------------- setup


def cmd_init(args) -> int:
    dest = Path(args.path or "manhwa-ocr.yaml")
    if dest.exists() and not args.force:
        raise UserError(f"{dest} already exists. Use --force to overwrite.")
    write_example_config(dest)
    _log(f"Wrote {dest}. It is picked up automatically when you run commands from this folder.")
    return 0


def cmd_start(args) -> int:
    """One-step onboarding for a new series: session + links in one go."""
    store = _store(args)
    try:
        sess = store.create(args.name, url_template=args.url_template or "")
    except FileExistsError:
        raise UserError(
            f"A series called '{args.name}' already exists.\n"
            f'  Switch to it:  python -m manhwa_ocr session use "{args.name}"\n'
            "  Add chapters:  python -m manhwa_ocr links add <url> ..."
        ) from None
    except ValueError as exc:
        raise UserError(str(exc)) from None

    _log(f"Started '{sess.name}' (folder: {sess.slug})")
    _log(f"  {sess.path}")

    urls = list(args.urls or [])
    if args.file:
        src = Path(args.file)
        if not src.exists():
            raise UserError(f"Link file not found: {src}")
        from .util import read_text_any

        urls += [ln.strip() for ln in read_text_any(src).splitlines() if ln.strip().startswith("http")]

    sess.ensure_links_file()
    added = sess.add_links(urls) if urls else 0
    _log("")
    if added:
        _log(f"Added {added} chapter link(s).")
        _log("Next:  python -m manhwa_ocr next")
    else:
        _log("Next: paste your chapter links, one per line, into")
        _log(f"  {sess.links_path}")
        _log("Or open the dashboard:  python -m manhwa_ocr ui")
    return 0


# ---------------------------------------------------------------- status


def cmd_next(args) -> int:
    from .status import next_steps

    for line in next_steps(_session(args)):
        _log(line)
    return 0


def cmd_status(args) -> int:
    sess = _session(args)
    links, problems = sess.parse_links()
    done = set(sess.chapters())
    pending = sess.pending()
    try:
        mem_line = SeriesMemory.load(sess.memory_path, sess.name).summary()
    except UserError as exc:
        mem_line = f"DAMAGED: {exc}"

    _log(f"Series:   {sess.name}")
    _log(f"Path:     {sess.path}")
    _log(f"Memory:   {mem_line}")
    _log(f"Recaps:   {len(sess.recaps())}")
    _log("")
    if problems:
        _log("Problems in chapters.txt:")
        for p in problems[:10]:
            _log(f"  ! {p}")
        _log("")
    if not links:
        _log("No chapter links yet. Paste them into:")
        _log(f"  {sess.links_path}")
        return 0
    _log(f"Chapters: {len(done & {c for c, _ in links})} done, {len(pending)} pending, {len(links)} total")
    if pending:
        _log(f"\nNext up:  chapter {pending[0][0]}   (details: python -m manhwa_ocr next)")
    else:
        _log("\nAll chapters transcribed.")
    return 0


# ---------------------------------------------------------------- provider mode


def cmd_run(args) -> int:
    if args.next or args.all_pending:
        pipe, sess = _build_pipeline(args)
        pending = sess.pending() if sess else []
        if not pending:
            _log("Nothing pending.")
            return 0
        todo = pending[:1] if args.next else pending
        _log(f"{len(todo)} chapter(s) to process\n")
        return _run_batch(pipe, [(c, u, None) for c, u in todo], args)

    if not args.chapter:
        raise UserError("Give --chapter N, or --next / --all-pending to use the link list.")

    pipe, sess = _build_pipeline(args)
    chapters = expand_chapters(args.chapter)
    links = dict(sess.read_links()) if sess else {}

    jobs = []
    for chapter in chapters:
        if args.folder:
            jobs.append((chapter, None, Path(args.folder)))
        elif args.url:
            if len(chapters) > 1 and "{n}" not in args.url:
                raise UserError("Multiple chapters need a URL with {n} in it, or use the link list with --all-pending.")
            jobs.append((chapter, args.url.replace("{n}", chapter), None))
        elif chapter in links:
            jobs.append((chapter, links[chapter], None))
        else:
            raise UserError(f"No link for chapter {chapter} in chapters.txt. Add it, or pass a URL or --folder.")
    return _run_batch(pipe, jobs, args)


def _run_batch(pipe, jobs, args) -> int:
    failures = 0
    for chapter, url, folder in jobs:
        _log(f"=== Chapter {chapter} ===")
        try:
            result = pipe.run_chapter(chapter=chapter, url=url, folder=folder, force=args.force, keep_work=args.keep)
            _log(f"  -> {result.output_path}")
        except (UserError, RuntimeError) as exc:
            failures += 1
            _log(f"  FAILED: {exc}")
            if args.strict:
                return 1
        _log("")
    _log(f"Done. {pipe.memory.summary()}")
    return 1 if failures else 0


# ---------------------------------------------------------------- agent mode


def cmd_prepare(args) -> int:
    from .workspace import clear_failure, record_failure

    flow, sess = _build_flow(args)
    chapter, url = args.chapter, args.url

    if args.next:
        if sess is None:
            raise UserError("--next needs a series (use --session or session use).")
        pending = sess.pending()
        if not pending:
            _log("No chapter links yet." if not sess.read_links() else "All chapters in the link list are done.")
            return 0
        chapter, url = pending[0]
        _log(f"Next pending chapter: {chapter}")
    if not chapter:
        raise UserError("Give --chapter N, or --next to take the next chapter from the link list.")
    chapter = require_chapter(chapter)
    if not url and not args.folder and sess is not None:
        url = dict(sess.read_links()).get(chapter)
        if not url:
            raise UserError(f"No link for chapter {chapter} in chapters.txt. Add one, or pass a URL or --folder.")

    _log(f"=== Preparing chapter {chapter} ===")
    try:
        task = flow.prepare(chapter=chapter, url=url, folder=Path(args.folder) if args.folder else None)
    except UserError as exc:
        # Recorded so `next` can offer the --folder workaround instead of
        # telling the agent to retry the same failing download forever.
        record_failure(flow.workdir, chapter, str(exc))
        raise
    clear_failure(flow.workdir, chapter)
    _log("")
    _log(f"Next: read {task}")
    _log("It lists the tiles, the prompt to apply, and where to write results.")
    return 0


def cmd_assemble(args) -> int:
    flow, _ = _build_flow(args)
    out = flow.assemble(chapter=args.chapter)
    _log(f"Next: answer {out}")
    return 0


def cmd_finalize(args) -> int:
    flow, sess = _build_flow(args)
    out = flow.finalize(chapter=args.chapter, keep_work=args.keep)
    _log(f"Wrote {out}")
    _log(flow.memory.summary())
    _log(f"Next:  python -m manhwa_ocr next{_pin(sess)}")
    return 0


# ---------------------------------------------------------------- recaps


def _recap_builder(args):
    from .script import RecapBuilder

    cfg, sess = _load_cfg(args)
    return sess, RecapBuilder(cfg, log=_log, session=sess.slug if sess else None)


def cmd_recap(args) -> int:
    sess, builder = _recap_builder(args)
    if sess:
        _log(f"Series:   {sess.name}")

    if args.cancel:
        _log("Recap task discarded." if builder.cancel() else "No recap task in progress.")
        return 0
    if args.finalize:
        written = builder.finalize()
        for p in written:
            _log(f"  wrote recaps/{p.name}")
        _log(f"{len(written)} recap(s) saved.")
        return 0

    if not 1 <= args.minutes <= 30:
        raise UserError("--minutes should be between 1 and 30.")
    transcribed = builder.transcribed()
    if not transcribed:
        raise UserError("No chapter transcripts yet. Run the OCR pipeline first.")
    chapters = expand_chapters(args.chapters) if args.chapters else transcribed

    if args.prepare:
        task = builder.prepare(chapters, args.minutes, force=args.force)
        if task is None:
            _log("Every requested chapter already has a recap. Use --force to rewrite.")
            return 0
        _log(f"Next: read {task}")
        _log(f"When the narration is written:  python -m manhwa_ocr recap --finalize{_pin(sess)}")
        return 0

    written = builder.generate(chapters, args.minutes, force=args.force)
    if not written:
        _log("Nothing new written.")
    else:
        _log(f"{len(written)} recap(s) saved to {builder.recaps_dir}")
    return 0


def cmd_compile(args) -> int:
    sess, builder = _recap_builder(args)
    recapped = builder.recapped()
    if not recapped:
        raise UserError("No recaps yet. Write some first:  python -m manhwa_ocr recap --chapters 1-3 --prepare")
    chapters = recapped if not args.chapters or args.chapters == "all" else expand_chapters(args.chapters)

    if args.bookends:
        try:
            _log("  writing intro and outro...")
            builder.generate_bookends(sort_chapters(chapters))
        except Exception as exc:  # noqa: BLE001 - bookends are optional
            _log(f"  ! skipped intro/outro: {exc}")

    out = builder.compile(chapters, name=args.name)
    _log(f"Wrote recaps/{out.name}")
    return 0


# ---------------------------------------------------------------- housekeeping


def cmd_import(args) -> int:
    """Copy transcripts made outside a session into it."""
    sess = _session(args)
    try:
        pattern = re.compile(args.pattern)
    except re.error as exc:
        raise UserError(f"--pattern is not a valid regular expression: {exc}") from None
    src_dir = Path(args.source)
    if not src_dir.is_dir():
        raise UserError(f"Folder not found: {src_dir}")
    if src_dir.resolve() == sess.path.resolve():
        raise UserError("That is the series' own folder; nothing to import.")

    imported, skipped = [], []
    for src in sorted(src_dir.glob("*.md")):
        m = pattern.fullmatch(src.name)
        if not m or not m.groups() or src.stat().st_size == 0:
            continue
        try:
            chapter = require_chapter(normalize_label(m.group(1)))
        except UserError:
            continue
        dest = sess.path / f"chapter_{chapter}.md"
        if dest.exists() and not args.force:
            skipped.append(chapter)
            continue
        shutil.copy2(src, dest)
        imported.append(chapter)

    if not imported and not skipped:
        raise UserError(f"No files in {src_dir} matched {args.pattern}")
    if imported:
        _log(f"Imported chapter(s): {', '.join(sort_chapters(imported))}")
    if skipped:
        _log(f"Already present, left alone: {', '.join(sort_chapters(skipped))}  (--force to overwrite)")
    return 0


def cmd_clean(args) -> int:
    from .workspace import clean_all, clean_finished, list_work

    sess = _session(args)
    work = list_work(sess.path)
    if not work:
        _log(f"Nothing to clean in '{sess.name}'.")
        return 0

    done = set(sess.chapters())
    total = sum(mb for _, mb in work)
    _log(f"Scratch files in '{sess.name}':")
    for chapter, mb in work:
        state = "transcribed, safe to delete" if chapter in done else "IN PROGRESS"
        _log(f"  ch{chapter:<6} {mb:>8.1f} MB   {state}")
    _log(f"  {'total':<8} {total:>8.1f} MB")

    if not args.yes:
        _log("")
        _log("Re-run with --yes to delete scratch for transcribed chapters.")
        _log("In-progress chapters are kept unless you add --all (that discards unfinished tile reading).")
        return 1
    count, freed = clean_all(sess.path) if args.all else clean_finished(sess.path)
    _log("")
    _log(f"Removed scratch for {count} chapter(s), freed {freed:.1f} MB.")
    return 0


def cmd_links(args) -> int:
    sess = _session(args)
    from .util import read_text_any

    if args.action == "add":
        if not args.urls:
            raise UserError("Give one or more URLs, or use:  links import --file links.txt")
        added = sess.add_links(args.urls)
        _log(f"Added {added} link(s). {len(sess.read_links())} total.")
        if added < len(args.urls):
            _log(f"Skipped {len(args.urls) - added}: already listed, or not an http(s) link.")
        return 0

    if args.action == "import":
        src = Path(args.file) if args.file else None
        if not src or not src.exists():
            raise UserError("Usage: python -m manhwa_ocr links import --file links.txt")
        urls = [ln.strip() for ln in read_text_any(src).splitlines() if ln.strip().startswith("http")]
        added = sess.add_links(urls)
        _log(f"Imported {added} link(s) from {src}. {len(sess.read_links())} total.")
        return 0

    if args.action == "path":
        _log(str(sess.ensure_links_file()))
        return 0

    links, problems = sess.parse_links()
    if not links:
        _log(f"No chapter links yet. Paste them into:\n  {sess.links_path}")
    else:
        done, recapped = set(sess.chapters()), set(sess.recaps())
        _log(f"{len(links)} chapter link(s) in '{sess.name}'\n")
        for chapter, url in links:
            mark = "done   " if chapter in done else "pending"
            recap = " recap" if chapter in recapped else ""
            _log(f"  ch {chapter:<6} {mark}{recap:<6}  {url}")
        _log(f"\n{len([c for c, _ in links if c not in done])} pending")
    for p in problems:
        _log(f"  ! {p}")
    return 0


def cmd_session(args) -> int:
    store = _store(args)
    action = args.action

    if action == "new":
        if not args.name:
            raise UserError('Usage: python -m manhwa_ocr session new "<series name>"')
        try:
            sess = store.create(args.name, url_template=args.url_template or "")
        except FileExistsError:
            raise UserError(f'That series already exists. Switch to it: session use "{args.name}"') from None
        except ValueError as exc:
            raise UserError(str(exc)) from None
        _log(f"Created and activated '{sess.name}'  ({sess.path})")
        _log("Next: paste your chapter links:  python -m manhwa_ocr links path")
        return 0

    if action == "list":
        sessions = store.list()
        if not sessions:
            _log(f"No series yet under {store.root}")
            _log('Start one:  python -m manhwa_ocr start "Series Name"')
            return 0
        active = store.active()
        _log(f"Series in {store.root}\n")
        for s in sessions:
            marker = "*" if active and s.slug == active.slug else " "
            _log(f" {marker} {s.slug:<34} {s.summary():<30} {(s.last_used or '')[:10]}")
        _log("\n* = active")
        return 0

    if action == "use":
        if not args.name:
            raise UserError("Usage: python -m manhwa_ocr session use <name>")
        sess = _get_session(store, args.name)
        store.activate(sess.slug)
        _log(f"Active series: {sess.name}  ({sess.summary()})")
        return 0

    if action == "info":
        sess = _get_session(store, args.name) if args.name else store.active()
        if sess is None:
            raise UserError("No active series. Pick one:  python -m manhwa_ocr session use <name>")
        _log(f"Series:   {sess.name}")
        _log(f"Folder:   {sess.path}")
        _log(f"Created:  {sess.created_at or '-'}")
        _log(f"Last use: {sess.last_used or '-'}")
        _log(f"Chapters: {sess.summary()}")
        _log(f"Recaps:   {len(sess.recaps())}")
        try:
            _log(f"Memory:   {SeriesMemory.load(sess.memory_path, sess.name).summary()}")
        except UserError as exc:
            _log(f"Memory:   DAMAGED: {exc}")
        return 0

    if action == "rename":
        if not args.name or not args.new_name:
            raise UserError("Usage: python -m manhwa_ocr session rename <old> <new>")
        try:
            sess = store.rename(args.name, args.new_name)
        except KeyError as exc:
            raise UserError(str(exc).strip("'\"")) from None
        except (ValueError, FileExistsError) as exc:
            raise UserError(str(exc)) from None
        _log(f"Renamed to '{sess.name}' ({sess.slug})")
        return 0

    if action == "archive":
        if not args.name:
            raise UserError("Usage: python -m manhwa_ocr session archive <name> --yes")
        sess = _get_session(store, args.name)
        if not args.yes:
            _log(f"This moves '{sess.name}' aside, including {sess.summary()}.")
            _log("Nothing is deleted. Re-run with --yes to confirm.")
            return 1
        dest = store.archive(sess.slug)
        _log(f"Archived to {dest}")
        _log("Restore by moving that folder back into the sessions folder.")
        return 0
    return 1


def cmd_memory(args) -> int:
    cfg, sess = _load_cfg(args)
    path = cfg.workdir / "series_memory.json"

    if args.action == "path":
        _log(str(path))
        return 0
    if args.action == "show":
        mem = SeriesMemory.load(path, cfg.series_name)
        _log(mem.summary())
        _log("")
        _log(mem.as_briefing())
        return 0
    if not args.yes:
        raise UserError(f"This resets {path}. Re-run with --yes to confirm (a backup is kept).")
    if path.exists():
        # Timestamped, so a second reset can't overwrite the first backup.
        backup = path.with_name(f"series_memory.reset-{datetime.now():%Y%m%d-%H%M%S}.json")
        path.replace(backup)
        _log(f"Moved existing memory to {backup.name}")
    else:
        _log("No memory file to reset.")
    return 0


def cmd_video(args) -> int:
    from .video import build as vb

    cfg, sess = _load_cfg(args)
    if sess is None:
        raise UserError("Video works per series: use --session or `session use`.")
    action = args.action

    def chapters(default_all: bool = True) -> list:
        spec = args.chapters or args.chapter
        if spec:
            return expand_chapters(spec)
        if default_all:
            return sort_chapters(sess.chapters())
        raise UserError("Give --chapter N (or --chapters 1-4,11).")

    if action == "status":
        rows = [vb.chapter_status(sess.path, c, probe=True) for c in chapters()]
        if not rows:
            _log("No transcribed chapters yet.")
            return 0
        _log(f"{'ch':<6}{'recap':<8}{'tags':<7}{'audio':<10}{'panels':<8}{'align':<12}{'plan':<26}{'sync':<10}video")
        for r in rows:
            plan = r["plan"]
            align = r["alignment"]
            align_s = ("-" if not align else "BROKEN" if align["broken"]
                      else "FAIL" if align["validation_status"] != "PASS"
                      else f"{align['review_queue']} review" if align["review_queue"] else "ok")
            plan_s = "-" if not plan else ("BROKEN" if plan["broken"] else "old style: rebuild" if plan["outdated"] else
                                           f"{plan['sync']}{' reviewed' if plan['reviewed'] else ''}{' edited' if plan['edited'] else ''}")
            audit = r.get("sync_audit")
            if not audit:
                sync_s = "-"
            elif audit.get("broken"):
                sync_s = "BROKEN"
            elif not audit.get("fresh"):
                sync_s = "stale"
            elif audit.get("pass"):
                sync_s = "ok"
            else:
                wrong = audit.get("wrong_ratio")
                sync_s = f"FAIL{f' {wrong:.0%}' if wrong is not None else ''}"
            audio_s = f"{r['duration']:.0f}s" if r["duration"] else ("yes" if r["audio"] else "-")
            video_s = "-" if not r["video"] else ("ok" if r["video_up_to_date"] else "outdated")
            tags = f"{r['tagged']:.0%}" if r["tagged"] is not None else "-"
            _log(f"{r['chapter']:<6}{'yes' if r['recap'] else '-':<8}{tags:<7}{audio_s:<10}{r['panels'] or '-':<8}"
                 f"{align_s:<12}{plan_s:<25} {sync_s:<9}{video_s}")
        _log("\n'align' is video align's own check: a sentence it couldn't resolve with full "
            "confidence (never confirmed by you) shows as \"N review\" - open its pairing sheet "
            "(video pair) before trusting the video for that stretch.")
        _log("'sync' is the last sync-audit gate result for the current plan (ok / FAIL / stale; "
            "run  video sync-audit --chapter N  for the details).")
        _log(f"\nAudio goes in: {sess.path / 'audio'}  (chapter_N.mp3 / .wav / .m4a)")
        return 0

    if action == "panels":
        from .video.media import ensure_pages, pages_dir
        from .video.panels import build_panels, sheet_paths

        for c in chapters(default_all=False):
            if args.pages:
                from .images import import_pages, list_images

                import_pages(list_images(Path(args.pages)), pages_dir(sess.path, c), log=_log)
            ensure_pages(sess, c, cfg.download, log=_log)
            build_panels(sess.path, c, cfg.video.get("panels"), force=args.force, log=_log)
            for s in sheet_paths(sess.path, c):
                _log(f"  contact sheet: {s.relative_to(sess.path).as_posix()}")
        return 0

    if action in {"plan", "sync"}:
        for c in chapters(default_all=False):
            vb.make_plan(sess, c, cfg, force=args.force or action == "sync",
                         sync="off" if args.no_sync else "auto", fmt=args.format, duration=args.duration,
                         pages_from=Path(args.pages) if args.pages else None, log=_log)
            _log(f"  review: .work/video/ch{c}/REVIEW.md   then:  python -m manhwa_ocr video render "
                 f"--chapter {c}{_pin(sess)}")
        return 0

    if action == "intro":
        from .video import intro as intro_mod

        if args.prepare:
            intro_mod.prepare(sess, log=_log)
            _log("  next: write intro/intro.json, record audio/chapter_0.mp3, then:  "
                 f"python -m manhwa_ocr video intro{_pin(sess)}")
            return 0
        out = intro_mod.run(sess, cfg, sync="off" if args.no_sync else "auto", fmt=args.format,
                            preview=args.preview, workers=args.workers, plan_only=args.dry_run, log=_log)
        if out is not None:
            _log(f"Wrote {out.relative_to(sess.path).as_posix()}")
        return 0

    if action == "sync-audit":
        from .video import syncaudit

        for c in chapters(default_all=False):
            _log(f"=== Chapter {c} ===")
            syncaudit.run(sess.path, c, cfg, log=_log)
        return 0

    if action == "pair":
        from .video import pairing

        for c in chapters(default_all=False):
            if args.apply:
                pairing.apply(sess.path, c, log=_log)
                _log(f"  next:  python -m manhwa_ocr video plan --chapter {c} --force --session \"{sess.slug}\"")
                continue
            task = pairing.write_task(sess.path, c, sess.slug, log=_log)
            problems = pairing.check(sess.path, c)
            _log(f"  {len(problems)} problem(s) found automatically" + (":" if problems else ""))
            for line in problems[:15]:
                _log(f"    - {line}")
            _log(f"  check the pictures: {task.relative_to(sess.path).as_posix()}")
        return 0

    if action == "align":
        from .video import align_engine

        for c in chapters(default_all=False):
            result = align_engine.build_alignment(sess.path, c, cfg,
                                                  sync="off" if args.no_sync else "auto", log=_log)
            segs = result["segments"]
            counts = {}
            for s in segs:
                counts[s["narration_type"]] = counts.get(s["narration_type"], 0) + 1
            resolved = sum(1 for s in segs if not s["needs_review"])
            queue = result["review_queue"]
            _log(f"\n=== Chapter {c} alignment ({result['sync_mode']}) ===")
            _log(f"  segments: {len(segs)}")
            for t in ("VISUAL_ACTION", "INTERNAL", "PURE_AUDIO", "SAME"):
                _log(f"    {t}: {counts.get(t, 0)}")
            _log(f"  resolved automatically: {resolved}")
            _log(f"  sent to review: {len(queue)}")
            if queue:
                _log("\n  review queue:")
                by_n = {s["sentence"]: s for s in segs}
                for n in queue:
                    s = by_n[n]
                    _log(f"    #{n}  {s['start']:.1f}s  tagged={s['tagged_panels']}  "
                        f"window={s['candidate_window']}  reason={s['confidence']}")
                    _log(f"        {s['text'][:90]}")
            v = result["validation"]
            _log(f"\n  validation: {v['status']}")
            for name, status in v["checks"].items():
                if status != "PASS":
                    _log(f"    FAIL: {name}")
            for p in v["problems"][:15]:
                _log(f"      sentence {p['sentence']} [{p['check']}]: {p['detail']}")
            held = [s for s in segs if s["narration_type"] in {"INTERNAL", "PURE_AUDIO", "SAME"} and s["tagged_panels"]]
            if held:
                _log("\n  previously force-matched, now held instead (no panel search attempted):")
                for s in held[:10]:
                    _log(f"    #{s['sentence']}  was tagged {s['tagged_panels']} -> now HOLD_PREVIOUS "
                        f"({s['narration_type']}: {s['type_reason']})")
            _log(f"\n  wrote {align_engine.alignment_path(sess.path, c).relative_to(sess.path).as_posix()}")
        return 0

    if action == "storyboard":
        from .video.panels import load_panels
        from .video.plan import load_plan
        from .video.storyboard import write_review_task, write_storyboard

        for c in chapters(default_all=False):
            panels = load_panels(sess.path, c) or {"panels": []}
            plan = load_plan(sess.path, c, {p["id"] for p in panels["panels"]})
            for p in write_storyboard(sess.path, c, plan, log=_log):
                _log(f"  {p.relative_to(sess.path).as_posix()}")
            write_review_task(sess.path, c, plan, sess.slug)
            from .video.review import write_narration

            _log(f"  narration: {write_narration(sess.path, c, plan).relative_to(sess.path).as_posix()}")
        return 0

    if action == "verify":
        from .video import verify as vf

        for c in chapters(default_all=False):
            from .video.media import video_dir

            # make_plan writes chapter_N.verify.json as part of planning, and
            # rebuilds whenever its inputs changed -- so a second full plan
            # buys nothing. Only force a rebuild when no verify report exists
            # yet (e.g. it was deleted while the plan survived).
            vpath = video_dir(sess.path) / f"chapter_{c}.verify.json"
            vb.make_plan(sess, c, cfg, force=not vpath.exists(), sync="off" if args.no_sync else "auto",
                         fmt=args.format, log=_log)
            rep = json.loads(vpath.read_text(encoding="utf-8"))
            _log(f"=== Chapter {c}: checked {len(rep['panels'])} drawn panels ===")
            for pid, r in rep["panels"].items():
                if r["action"] != "ok":
                    _log(f"  {pid}: {r['action']} ({', '.join(r['problems']) or 'no character'})")
            _log(f"  dropped: {len(rep['dropped'])}   needs eyes: {len(rep['needs_eyes'])}")
            _log(f"  report: video/chapter_{c}.verify.json")
        return 0

    if action == "review":
        from .video import review as rv

        for c in chapters(default_all=False):
            rv.run_review(sess, c, cfg, dry_run=args.dry_run, max_rounds=args.max_rounds or 3, log=_log)
        return 0

    if action == "approve":
        from .video import review as rv
        from .video.panels import load_panels
        from .video.plan import load_plan

        for c in chapters(default_all=False):
            ids = {p["id"] for p in (load_panels(sess.path, c) or {"panels": []})["panels"]}
            plan = load_plan(sess.path, c, ids)
            rv.approve(sess.path, c, plan, note="approved by hand")
            _log(f"  chapter {c}: approved; render with  video render --chapter {c}")
        return 0

    if action == "render":
        for c in chapters(default_all=False):
            _log(f"=== Chapter {c} ===")
            vb.render_chapter(sess, c, cfg, preview=args.preview, workers=args.workers,
                              allow_estimated=args.force, allow_unreviewed=args.force,
                              allow_sync_fail=args.force, strict=args.strict, log=_log)
        return 0

    if action == "build":
        results = vb.build(sess, chapters(), cfg, force=args.force, sync="off" if args.no_sync else "auto",
                           fmt=args.format, preview=args.preview, workers=args.workers, log=_log)
        _log("")
        for c, r in results.items():
            _log(f"  ch {c:<6} {r}")
        return 1 if any(r.startswith("failed") for r in results.values()) else 0

    if action == "clip":
        if args.start is None or args.end is None:
            raise UserError("Give the range in seconds:  video clip --chapter 3 --from 45 --to 105 [--format 9:16]")
        (c,) = chapters(default_all=False)[:1]
        out = vb.make_clip(sess, c, cfg, args.start, args.end, fmt=args.format, preview=args.preview,
                           workers=args.workers, log=_log)
        _log(f"Wrote {out.relative_to(sess.path).as_posix()}")
        return 0

    if action == "compile":
        vb.compile_videos(sess, chapters(), titles=not args.no_titles, name=args.name, log=_log)
        return 0
    return 1


def cmd_ui(args) -> int:
    from .ui.server import serve

    return serve(port=args.port, sessions_root=getattr(args, "sessions_root", None),
                 open_browser=not args.no_browser, log=_log)


# ---------------------------------------------------------------- parser


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="manhwa-ocr",
        description="Transcribe webtoon chapters with a vision model, keeping character "
        "and plot context across chapters, and write recap narration.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    def ws(p):
        p.add_argument("--session", help="series to act on (default: the active one)")
        p.add_argument("--sessions-root", dest="sessions_root", help="where series folders live")
        p.add_argument("--workdir", help="advanced: bypass sessions and use this folder")
        p.add_argument("--series", help="override the series display name")
        p.add_argument("--config", help="config YAML (default: ./manhwa-ocr.yaml if present)")

    def sess_only(p):
        p.add_argument("--session", help="series to act on (default: the active one)")
        p.add_argument("--sessions-root", dest="sessions_root")

    p = sub.add_parser("init", help="write an example config file")
    p.add_argument("path", nargs="?")
    p.add_argument("--force", action="store_true")
    p.set_defaults(func=cmd_init)

    p = sub.add_parser("start", help="begin a new series: its own folder, memory and recaps")
    p.add_argument("name")
    p.add_argument("urls", nargs="*", help="chapter URLs, in reading order")
    p.add_argument("--file", help="text file of chapter URLs")
    p.add_argument("--url-template", dest="url_template")
    p.add_argument("--sessions-root", dest="sessions_root")
    p.set_defaults(func=cmd_start)

    p = sub.add_parser("next", help="exactly what to do next (safe to run any time)")
    sess_only(p)
    p.set_defaults(func=cmd_next)

    p = sub.add_parser("status", help="progress summary")
    sess_only(p)
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("run", help="provider mode: transcribe chapters with an API key")
    g = p.add_mutually_exclusive_group()
    g.add_argument("url", nargs="?", help="chapter URL, or template with {n}")
    g.add_argument("--folder", help="use local images instead of downloading")
    p.add_argument("--chapter", help="e.g. 3, or 1-7, or 1-4,11")
    p.add_argument("--next", action="store_true", help="next pending chapter from the link list")
    p.add_argument("--all-pending", dest="all_pending", action="store_true")
    ws(p)
    p.add_argument("--provider", help="anthropic | gemini | openai | ollama")
    p.add_argument("--model", help="vision model id")
    p.add_argument("--base-url", dest="base_url")
    p.add_argument("--api-key-env", dest="api_key_env", help="env var holding the key")
    p.add_argument("--force", action="store_true", help="redo chapters already written")
    p.add_argument("--strict", action="store_true", help="stop on first failure")
    p.add_argument("--keep", action="store_true", help="keep downloaded pages and tiles")
    p.set_defaults(func=cmd_run)

    p = sub.add_parser("prepare", help="agent mode 1/3: download + slice, write TASK.md")
    g = p.add_mutually_exclusive_group()
    g.add_argument("url", nargs="?")
    g.add_argument("--folder", help="use local images instead of downloading")
    p.add_argument("--chapter")
    p.add_argument("--next", action="store_true", help="next pending chapter from the link list")
    ws(p)
    p.set_defaults(func=cmd_prepare)

    p = sub.add_parser("assemble", help="agent mode 2/3: merge tile results, write ASSEMBLE.md")
    p.add_argument("--chapter", required=True)
    ws(p)
    p.set_defaults(func=cmd_assemble)

    p = sub.add_parser("finalize", help="agent mode 3/3: write the chapter, update memory")
    p.add_argument("--chapter", required=True)
    p.add_argument("--keep", action="store_true", help="keep downloaded pages and tiles")
    ws(p)
    p.set_defaults(func=cmd_finalize)

    p = sub.add_parser("recap", help="write recap narration, one file per chapter")
    p.add_argument("--chapters", help="e.g. 3, or 1-4, or 1-4,11 (default: all transcribed)")
    p.add_argument("--minutes", type=float, default=6.0, help="target minutes per chapter")
    p.add_argument("--prepare", action="store_true", help="agent mode: write prompts for the agent")
    p.add_argument("--finalize", action="store_true", help="agent mode: save what the agent wrote")
    p.add_argument("--cancel", action="store_true", help="agent mode: discard an unfinished task")
    p.add_argument("--force", action="store_true", help="rewrite existing recaps")
    ws(p)
    p.set_defaults(func=cmd_recap)

    p = sub.add_parser("compile", help="combine per-chapter recaps into one video script")
    p.add_argument("--chapters", help="e.g. 1-4,11 or all (default: all recapped)")
    p.add_argument("--name", help="output file name")
    p.add_argument("--bookends", action="store_true", help="also write intro/outro (needs an API key)")
    ws(p)
    p.set_defaults(func=cmd_compile)

    p = sub.add_parser("import", help="copy existing transcripts into the series")
    p.add_argument("source", help="folder with the transcript files")
    p.add_argument("--pattern", default=r"chapter_(\d+(?:\.\d+)?)(?:_ocr)?\.md",
                   help=r"filename regex; group 1 is the chapter")
    p.add_argument("--force", action="store_true", help="overwrite existing transcripts")
    sess_only(p)
    p.set_defaults(func=cmd_import)

    p = sub.add_parser("video", help="recap videos: panels, plan, sync, render, clips")
    p.add_argument("action", choices=["status", "panels", "pair", "align", "plan", "sync", "sync-audit", "storyboard", "render",
                                      "build", "clip", "compile", "verify", "review", "approve", "intro"])
    p.add_argument("--chapter", help="one chapter")
    p.add_argument("--chapters", help="several, e.g. 1-4,11 (default for build/status: all transcribed)")
    p.add_argument("--force", action="store_true", help="rebuild even if up to date (discards plan edits)")
    p.add_argument("--preview", action="store_true", help="fast 480p render for checking")
    p.add_argument("--strict", action="store_true",
                   help="render: stop if any sentence is flagged for review (default: warn only)")
    p.add_argument("--format", choices=["16:9", "9:16"], help="video shape (default from config: 16:9)")
    p.add_argument("--no-sync", dest="no_sync", action="store_true", help="don't transcribe the audio")
    p.add_argument("--duration", type=float, help="plan without audio, for this many seconds")
    p.add_argument("--pages", help="folder of page images to use for this chapter")
    p.add_argument("--from", dest="start", type=float, help="clip start, seconds")
    p.add_argument("--to", dest="end", type=float, help="clip end, seconds")
    p.add_argument("--workers", type=int, default=0, help="parallel renders (default: half your CPU cores)")
    p.add_argument("--no-titles", dest="no_titles", action="store_true", help="compile without title cards")
    p.add_argument("--name", help="compiled output name")
    p.add_argument("--apply", action="store_true", help="pair: save the fixed tags from pairs.json")
    p.add_argument("--prepare", action="store_true",
                   help="intro: write a template intro/intro.json and contact sheets of every chapter's drawn panels")
    p.add_argument("--dry-run", dest="dry_run", action="store_true", help="review: ask the reviewer, apply nothing; intro: plan only, no render")
    p.add_argument("--max-rounds", dest="max_rounds", type=int, default=None,
                   help="review: how many review-and-fix rounds to allow (default 3)")
    ws(p)
    p.set_defaults(func=cmd_video)

    p = sub.add_parser("guard", help="lock the program code so AI agents can't change it")
    p.add_argument("action", choices=["status", "lock", "unlock"])
    p.set_defaults(func=cmd_guard)

    p = sub.add_parser("ui", help="open the local dashboard")
    p.add_argument("--port", type=int, default=8765)
    p.add_argument("--no-browser", action="store_true")
    p.add_argument("--sessions-root", dest="sessions_root")
    p.set_defaults(func=cmd_ui)

    p = sub.add_parser("clean", help="delete downloaded pages and tiles")
    sess_only(p)
    p.add_argument("--yes", action="store_true", help="confirm deletion")
    p.add_argument("--all", action="store_true", help="include chapters still in progress")
    p.set_defaults(func=cmd_clean)

    p = sub.add_parser("links", help="the series' chapter link list")
    p.add_argument("action", choices=["add", "list", "import", "path"])
    p.add_argument("urls", nargs="*")
    p.add_argument("--file")
    sess_only(p)
    p.set_defaults(func=cmd_links)

    p = sub.add_parser("session", help="manage series")
    p.add_argument("action", choices=["new", "list", "use", "info", "rename", "archive"])
    p.add_argument("name", nargs="?")
    p.add_argument("new_name", nargs="?")
    p.add_argument("--url-template", dest="url_template")
    p.add_argument("--sessions-root", dest="sessions_root")
    p.add_argument("--yes", action="store_true")
    p.set_defaults(func=cmd_session)

    p = sub.add_parser("memory", help="inspect or reset the series bible")
    p.add_argument("action", choices=["show", "path", "reset"])
    ws(p)
    p.add_argument("--yes", action="store_true")
    p.set_defaults(func=cmd_memory)

    return parser


def cmd_guard(args) -> int:
    from . import guard

    if args.action == "lock":
        guard.lock(log=_log)
    elif args.action == "unlock":
        guard.unlock(log=_log)
    else:
        problems = guard.changes()
        if not guard.is_locked():
            _log("Unlocked: the program can be edited. Lock it with:  python -m manhwa_ocr guard lock")
        elif problems:
            _log("LOCKED, but the program was changed:\n  " + "\n  ".join(problems))
            return 2
        else:
            _log("Locked and intact: the program is exactly as you locked it.")
    return 0


def main(argv: list[str] | None = None) -> int:
    _setup_console()
    args = build_parser().parse_args(argv)
    try:
        if args.func is not cmd_guard:
            from .guard import check

            check()  # never run a program an agent has modified
        return args.func(args) or 0
    except KeyboardInterrupt:
        _log("\nInterrupted. Everything written so far is saved; re-run the same command to continue.")
        return 130
    except UserError as exc:
        _log(f"Error: {exc}")
        return 2
    except Exception as exc:  # noqa: BLE001 - last-resort, keep agents unblocked
        from .providers import ProviderError

        if os.environ.get("MANHWA_DEBUG"):
            traceback.print_exc()
        if isinstance(exc, ProviderError):
            _log(f"Error: {exc}")
            _log("No API key? Use agent mode instead:  python -m manhwa_ocr next")
            return 2
        if isinstance(exc, FileNotFoundError):
            _log(f"Error: file not found: {exc.filename or exc}")
            return 2
        _log(f"Unexpected error: {type(exc).__name__}: {exc}")
        _log("Set MANHWA_DEBUG=1 and re-run to see the full traceback.")
        return 1


if __name__ == "__main__":
    sys.exit(main())
