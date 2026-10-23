---
name: make-recap-video
description: Take a manhwa chapter all the way from its link to a finished recap video, with Deepgram synthesizing the narration instead of a human recording. Follows docs/FULL_PIPELINE_WORKFLOW.md then docs/CHAPTER_BUILD_WORKFLOW.md exactly. Use whenever the user asks for a fully-automated, no-recording, end-to-end chapter video build, or mentions wanting Deepgram/TTS to drive the whole pipeline.
---

# Build a chapter video end-to-end, Deepgram narrating

This skill exists for the same reason `/build-chapter` exists: the detailed
phase-by-phase steps fade from a long session's working memory, so they're
read fresh from file every time, never from memory. This skill covers the
FULL pipeline (link to video); `/build-chapter` alone only covers the
audio-to-video half.

## What to do when this skill is invoked

1. **Parse `args` for series name, chapter number(s), and anything else
   mentioned** (a specific defect to re-check, a config override) — same
   parsing rules as `/build-chapter`'s skill file, reused here:
   - Series name: stated, or the active session (`session list`) — but pass
     `--session "<name>"` explicitly on every command regardless.
   - Chapter(s): a number, a range, or "the next one" (`status`/`next`).
   - There is no audio file to look for here — Deepgram generates it.

2. **Read `docs/FULL_PIPELINE_WORKFLOW.md` in FULL, every time this skill
   runs.** Do not paraphrase from memory.

3. **Follow it exactly**, stage by stage, substituting the parsed series/
   chapter values. Stage 5 of that file tells you to then read
   `docs/CHAPTER_BUILD_WORKFLOW.md` in full as well — do that; don't treat
   it as already covered.

4. If a chapter number or series couldn't be determined, ask the user rather
   than guessing.

5. If asked for several chapters, run the full stage list (both files)
   **separately, per chapter** — never batch a stage across chapters.

## Example invocations

- `/make-recap-video Monster's Streamer Star, chapter 3` → full pipeline,
  that series, chapter 3.
- `/make-recap-video continue` (series/chapter already established earlier
  in the conversation) → next pending chapter for that series.
- `/make-recap-video chapters 2-3` → run all stages for chapter 2 in full,
  re-read both workflow files, then run all stages for chapter 3.
