# TODO — video/wide-mode-v2 (read this first, next session)

Last updated: 2026-10-21. Branch `video/wide-mode-v2`, tip `d076435`, pushed and
matching origin as of this update. Full test suite: 705 passed, 1 skipped,
0 failed.

## Done since last update

- **Durable chapter-build workflow.** `docs/CHAPTER_BUILD_WORKFLOW.md` is now
  the single source of truth for building/continuing a chapter video (was:
  a Downloads-folder file the user had to paste each time, which faded from
  memory across a multi-chapter continuation session). `AGENTS.md` points
  to it from inside "## Making videos"; `docs/AGENT_WAY.md` documents it as
  Prompt I; `USER_GUIDE.md`'s prompt table lists it. A `/build-chapter`
  skill (`.claude/skills/build-chapter/SKILL.md`) parses the series name,
  chapter number(s) and audio file straight from what the user types and
  re-reads the workflow file fresh on every invocation — no more manually
  filling in template blanks, and no reliance on conversation memory.

## Remaining work

1. **New smooth motion/editing tools.** Discussed but never built this
   session — only existing motion (push_in/pull_out/pan_*) was fixed/hardened,
   nothing new was added. Needs: a short design pass on what new motion
   primitives would actually help (e.g. a diagonal pan+zoom combo), kept
   restrained/smooth per the user's explicit "no jarring edits" instruction,
   then implementation + tests, same rigor as the wide-mode work.

2. **Push `backup/unsure-code` branch.** Local commit `d3ad85c` ("WIP: Backup
   of uncertain code changes") exists only locally — pushing it was blocked by
   the permission system as an "out-of-place publication" (a brand-new branch).
   Needs explicit user confirmation before retrying `git push -u origin
   backup/unsure-code`.

3. **`chapter_build_prompt.md`'s full workflow, done formally.** This session
   did the spirit of Phases 3/4/6 (window audit, gates, deep review) on
   chapter 1 wide-mode, but never produced:
   - Phase 1's formal sentence-by-sentence audio↔video mapping report
     (list every sentence + tagged panel + confirm it shows during its
     window; list every never-shown panel + skip/add decision).
   - A final 1080p render under a FRESH filename (only 480p preview renders
     were done this session, for speed, per later instruction not to
     re-render repeatedly).
   - Delivery via "Muse storage" — unclear what this is; ask the user.

4. **Classic mode's own crop picker still has the original cutoff bug.**
   The figure-extension/reposition hardening only applies to wide-mode
   (`wide_display`) and adaptive-mode classic-style (`wide_fill`) cards.
   Plain `video.mode: classic` (the default, untouched) still uses
   `pick_card()`'s bubble-avoidance-only crop with no figure-containment
   check — same class of bug as the dragons, just never reported because
   classic mode wasn't under test this session. Worth a deliberate decision:
   backport the hardening into classic's own crop picker, or leave classic
   mode alone by design (it predates this whole effort).

## Explicitly deferred (not on this list, don't pick up without asking)

- **Music/SFX feature.** Full planning doc already written and saved at
  `C:\Users\akash\Downloads\music_sfx_plan.docx` (sourcing list, tone-
  detection design, segment-trimming algorithm, file-by-file integration
  plan, phased build order). User said: remove from the active to-do,
  revisit later. Don't start building this without being asked again.

## House rules for whoever (agent or human) picks this up next

- Never commit/push to `main`. Work stays on `video/wide-mode-v2` (or a new
  branch off it) until the user says otherwise.
- Run the full suite (`python -m pytest tests/ -q`) before AND after any
  change; it should stay at "N passed, 1 skipped, 0 failed" (the 1 skip is
  a pre-existing environment-specific ffmpeg/permission thing, not a bug
  this branch owns).
- Prefer the STORYBOARD (`video/chapter_N.storyboard_0*.png`, written by
  `video plan`) over rendering a full video to check crops/framing — it's
  the same crop data the renderer uses, just much faster to produce and to
  look at. Only render when you need to check motion/timing/audio, which a
  storyboard can't show.
- Any new wide/adaptive-mode bug: check `_wide_style_card` (plan.py) first —
  it's the single shared implementation both modes use, so a fix there
  fixes both at once. Don't patch `_wide_pass` and `_adaptive_pass`
  separately; they're thin wrappers around it on purpose.
