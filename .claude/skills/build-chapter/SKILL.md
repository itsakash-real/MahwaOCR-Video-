---
name: build-chapter
description: Build or continue a wide/adaptive-mode chapter video for a manhwa-ocr series, following docs/CHAPTER_BUILD_WORKFLOW.md exactly. Use whenever the user asks to build, continue, or re-review a chapter video on video/wide-mode-v2 (or asks for the next chapter in a multi-chapter session). Parses the series name, chapter number(s), and audio file from whatever the user typed — they never need to edit a template by hand.
---

# Build/continue a chapter video

This skill exists so the detailed build workflow is read fresh every time
it's invoked, never relying on what's still "remembered" from earlier in a
long, multi-chapter session. It is a thin wrapper: the actual phase-by-phase
steps live in `docs/CHAPTER_BUILD_WORKFLOW.md`, not here, so there is exactly
one place to keep that workflow up to date.

## What to do when this skill is invoked

1. **Parse `args` (whatever the user typed when invoking this skill) for:**
   - **Series name.** If not stated, use the session already active for
     `--session` (check with `python -m manhwa_ocr session list`) — but
     still pass `--session "<name>"` explicitly on every command from then
     on; never omit it even once it's "the active one".
   - **Chapter number(s).** A single chapter ("build chapter 2"), a range
     ("chapters 2-4"), or "the next one" (run
     `python -m manhwa_ocr status --session "<name>"` or
     `next --session "<name>"` to find it). If several chapters are given,
     you will run the full workflow once per chapter — see the workflow
     file's own "Multiple chapters in one request" section.
   - **Audio file.** If not stated, assume the project's own default,
     `<session>/audio/chapter_<N>.<ext>` — check the session's `audio/`
     folder for whichever extension (`.mp3`/`.wav`/`.m4a`) actually exists
     for that chapter number. If the user pasted a specific path, use that
     instead. If no audio file exists for that chapter at all, stop and
     tell the user exactly which path is missing — never create, convert,
     or generate audio (AGENTS.md).
   - Anything else the user mentioned (a config path, a specific mode, a
     specific defect to re-check) — carry it through as extra context for
     the workflow, it doesn't need its own parsing rule.

2. **Read `docs/CHAPTER_BUILD_WORKFLOW.md` in FULL, every single time this
   skill runs** — even if you (or another invocation of this skill) already
   built an earlier chapter a few messages ago in this same conversation.
   Do not paraphrase it from memory; open and re-read the actual file.

3. **Follow it exactly**, substituting the values parsed in step 1 wherever
   it says `<N>`, `<SERIES>`, or refers to the audio file. Do not ask the
   user to fill in a template themselves — that manual-edit step is exactly
   what this skill replaces.

4. If a chapter number, series, or audio path couldn't be determined from
   `args` or the session state, ask the user directly rather than guessing
   — a wrong `--session` or chapter number silently works on the wrong
   series' data.

## Example invocations and how they parse

- `/build-chapter Monster's Streamer Star, chapter 3` → series = "Monster's
  Streamer Star", chapter = 3, audio = default path for chapter 3.
- `/build-chapter chapter 4, audio is audio/ch4_final.m4a` → series = active
  session, chapter = 4, audio = the explicit path given.
- `/build-chapter continue` (series already set from earlier in the
  conversation) → series = whatever was already established, chapter = next
  pending one per `status`/`next`, audio = default path.
- `/build-chapter chapters 2-3` → run the full workflow for chapter 2, then
  re-read the workflow file again, then run it in full for chapter 3.
