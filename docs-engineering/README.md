# Docs Engineering — Manhwa Recap Pipeline

**Purpose.** This folder is the single written record of how the Infinite Titles /
Genius Knight manhwa recap pipeline actually works, as built and measured in
October 2026. A brand-new AI agent, or a new session with no prior context,
should be able to read these files and reproduce the pipeline without guessing.

**Rule about these docs.** Every rule, number, filename, and step in this pack
is traceable to a source file listed in the References section of each
document. If a fact could not be verified from a file actually read, it is
marked **UNVERIFIED** or omitted. Do not fill gaps with plausible inventions.
If a source file and this pack disagree, trust the source file, fix this pack,
and say so.

## How to read this pack

| Job you are starting | Read first | Then read |
|---|---|---|
| I am new, explain the whole thing | This README, then `05-full-build-workflow.md` | The stage file for the stage you are doing |
| Transcribe a chapter | `01-transcription.md` | `architecture-transcription.md` |
| Write / refine a recap script | `02-recap.md` | `architecture-recap.md` |
| Voice audio for a chapter | `03-audio.md` | `architecture-audio.md` |
| Map, render, gate, and verify a video | `04-video.md` | `architecture-video.md` |
| Run the whole end-to-end build | `05-full-build-workflow.md` | The four stage files, in order |

The numbered files (`01`–`05`) are **procedures**: step-by-step, with inputs,
outputs, file paths, and failure handling. The `architecture-*` files are
**design documents**: data-flow diagrams (in text), workflows, system design,
invariants, and failure modes for the same stage. Procedures tell you *what to
do*; architecture files tell you *why the system is shaped that way* and what
breaks if you change it.

## The pipeline at a glance

```
Chapter pages (AsuraScans)
  → 01-transcription  → verified transcript (transcripts/chapter_N_verified.md)
  → 02-recap          → recap → Gemini-refined script (refined/chapter_N_refined.md)
  → 03-audio          → narration audio + manifest (audio/chapter_N/audio.wav)
  → 04-video          → mapping → shots → render → gates → re-verification
                      → chapter video → (coordinator only) stream-copy concat
                      → continuous movie
  → Delivery          → Google Drive folder "Infinite Titles", after ALL verification
```

One chapter at a time through transcription → verification → recap →
refinement → audio, in pipeline order. No half-written chapter left behind.
Video work for a set of chapters runs as one coordinated go after that set's
audio is complete, with one worker per chapter in its own workdir, then a
coordinator-only concat.

## Standing rules — apply to ALL stages

These rules override speed, convenience, and any shortcut a tool offers.

1. **User-supplied exact scripts are never edited wholesale.** If a script
   issue is found (as in Infinite Titles Chapter 1, where two paragraphs
   truncated in audio), fix **only that affected part/paragraph** and re-voice
   **only that paragraph**. Never rewrite the whole script, never re-voice the
   whole chapter for a local fix. Keep the original file untouched; write the
   fixed version to a new file (the `_fixed` / `_pretrim` pattern in
   `refined/`).
2. **Never silently substitute panels.** The video uses approved subject-window
   cuts only. If an approved cut cannot be used, **STOP and tell the user**.
   Never substitute a pipeline panel, a re-detected panel, or a panelfit crop
   in its place. This rule exists because a substitution shipped once (Genius
   Chapter 1 v1: pipeline panels used for 43 approved cuts, ~60% white,
   ~520 px pillar) and had to be rejected.
3. **Never `--force` past a failed gate.** A failed gate is a stop, not a
   warning. Fix the window, the pair, or the cut, re-render the affected
   shot(s), and re-check the fixed shots **and their neighbours**. Repeat to
   0 FAILs. "0 FAILs" must never be reported as "no issues" — residues are
   listed openly.
4. **Never paste a video in chat as an attachment.** Chat attachments are
   preview/streaming in some clients, are not reliably downloadable, and may
   be transcoded. Video delivery is by original-file download only.
5. **Drive delivery only after verification.** The Google Drive folder
   "Infinite Titles" receives a chapter's files **only once all is done and
   verified**. Contents are video + transcript + recap script + audio ONLY —
   never images, panels, or cuts. Do not upload drafts. Do not upload a
   partial set.
6. **Hardcode decisions into versioned code, pinned config, and style files.**
   Never leave a style, voice, placement, or gating decision only in an
   agent's head or in memory. One branch per fix effort; open a pull request
   only when the user says merge; never touch `main` or the current branch —
   with the single recorded exception in §Repo rules below.
7. **Never quote a number from a handoff or summary without re-reading it
   from disk or logs.** Handoff previews have fabricated numbers before.
   Re-read `process.log`, gate JSON, or the artifact file itself before
   committing to a figure.
8. **Gemini ban-safety (highest priority).** Gemini calls are strictly
   sequential, one key at a time, rotate **only** on a 429, with exponential
   backoff and a circuit breaker. Never blast calls in parallel — quota is
   per project, not per key. Model: `gemini-3.5-flash-lite`. Key files live
   at `~/.config/manhwa/gemini-*.key` (600 permissions). **Key values are
   never recorded or printed, anywhere.**
9. **Fresh filename for every delivered video.** Never reuse a filename — the
   user's player caches old files.
10. **Report true verified status, whatever it is.** If a deadline and a
    quality gate conflict, quality wins: report the delay, do not ship a soft
    or unverified video to hit the clock.

## Repo rules

- Work on **one branch** per fix effort; open a PR only when the user says
  merge; never push without authorisation.
- Push auth: pushing to `itsakash-real/MahwaOCR-Video-.git` fails with 403
  as `akashmaurya3160`; switch `gh auth` to `itsakash-real` and run
  `gh auth setup-git` before pushing.
- **Single recorded exception (2026-10-27):** the user directly ordered the
  Genius Knight movie style port to `main` (commit `ea90739`,
  "feat(video): Genius Knight movie style replaces main long-form movie
  mode"). That order covered that port only. It is **not** a general
  permission to touch `main` or to skip PRs for other work.
- **Checkout note (verified 2026-10-27):** the local checkout at
  `~/workspace/MahwaOCR-Video-/` is on branch `fix/video-vertical-motion`,
  and `manhwa_ocr/video/framepipe.py` is **not present** in that checkout.
  The framepipe consolidation lives on origin `main` (commit `ea90739`).
  Until a `main` checkout is used, the per-chapter session scripts
  (`frame_pipe_render_chN.py` and friends, in the session workdirs) are the
  runnable source of truth for rendering, and `docs/GENIUS_MOVIE_STYLE.md`
  on `main` is the spec. Do not claim to have read `framepipe.py` locally
  when the checkout does not contain it.

## Series scopes — do not mix them

These are separate series with separate locked settings. A setting locked for
one series is not a setting for another.

| Series | Voice | Gap | Notes |
|---|---|---|---|
| Infinite Titles (long-form) | Deepgram `aura-2-odysseus-en` @ native **1.3** | 0.25 s between paragraphs (locked audio format) | This pack's main subject |
| Genius Knight (long-form) | Deepgram `aura-2-odysseus-en` @ 1.3 | 0.11 s (Genius chapters already locked that way) | The video style this pack standardises |
| Solo Swordmaster | Deepgram `aura-2-odysseus-en` @ **1.35** | 0.11 s | Chapter-wise movies, stop order in effect — see §Status notes |
| Mangvoro Shorts | Deepgram `aura-2-orion-en` @ native 1.3× | — | Shorts, different pipeline, not covered by this pack's video file |

Solo Swordmaster status note: a user stop order (2026-10-24) is in effect —
no Solo Chapter 2 work until the user says go. That stop order is not lifted
by anything in this pack.

## Key measured numbers (so a new agent does not re-derive them wrong)

- Genius Knight, 6 chapters: **~12 hours total** (2026-10-26), roughly
  2 hours per chapter on average including mapping, TTS, render, gates,
  re-verification, and the combined movie. Render ≈ 2 hours for the set;
  verification ≈ 6 hours for the set. Verification is the long stage, not
  rendering.
- Infinite Titles Chapter 1 v2, Part 4 render (measured from its log):
  **1,693 frames (56.4 s of video) in 756 s wall time** on a 2-CPU VM with no
  GPU and 7.7 GB RAM — about 13× real time. A 423.59 s chapter therefore
  costs roughly 90 minutes of render time if rendered alone; parallel
  renders contend on the same 2 CPUs and slow each other down.
- Infinite Titles Chapter 1 v2 audio: **423.59 s** total (B method).
- Voice test, 1,439-word v1 script (measured, for that script only):
  A (per-sentence, native 1.3) **432.31 s**, B (per-paragraph, native 1.3)
  **408.20 s (winner)**, C (1.0 + ffmpeg `atempo=1.3`) **442.92 s (worst,
  forbidden)**. These are measurements for that script, not universal
  constants.
- Genius quality range: **1,278–1,419 kbps** total bitrate. Under ~1 Mbps
  is a quality FAIL even if the container says 1920×1080.
- Genius Chapter 1 motion median, measured on the same interior-difference
  method as the Infinite Titles deep review: **20.24**. A chapter median
  under 3.0 means motion has failed.

## File index

| File | What it is |
|---|---|
| `README.md` (this file) | Index, standing rules, scopes, measured numbers |
| `01-transcription.md` | Transcription procedure, step by step |
| `02-recap.md` | Recap and refinement procedure, step by step |
| `03-audio.md` | Audio procedure and all audio rules |
| `04-video.md` | Video procedure — the Genius system, in full detail |
| `05-full-build-workflow.md` | End-to-end architecture, flows, and timings |
| `architecture-transcription.md` | Transcription DFDs, workflows, system design |
| `architecture-recap.md` | Recap DFDs, workflows, system design |
| `architecture-audio.md` | Audio DFDs, workflows, system design |
| `architecture-video.md` | Video DFDs, workflows, system design |

## Sources read for this pack

- `~/workspace/ManhwaRecaps/genius-knight-fallen-race/work_movie/GENIUS_YESTERDAY_WORKFLOW_EXACT.md`
- `~/workspace/ManhwaRecaps/infinite-titles/AUDIO_FORMAT_LOCKED.md`
- `~/workspace/ManhwaRecaps/infinite-titles/VOICE_FIX_ODYSSEUS.md`
- `~/workspace/ManhwaRecaps/infinite-titles/REBUILD_CHECKLIST_NO_REPEAT.md`
- `~/workspace/ManhwaRecaps/infinite-titles/USER_ORDER_video_after_ch10.md`
- `~/workspace/ManhwaRecaps/infinite-titles/STATUS.md`
- `~/workspace/ManhwaRecaps/infinite-titles/user_scripts_exact/` (word counts
  and MD5s verified on disk, 2026-10-27 — see `02-recap.md` and `03-audio.md`)
- `~/workspace/MahwaOCR-Video-/manhwa_ocr/video/tts.py` (key resolution and
  Deepgram call shape, as actually coded)
- `~/workspace/MahwaOCR-Video-/docs/RECAP_STYLE_MANHWABRO.md`
- `~/workspace/ManhwaRecaps/infinite-titles/work_movie/ch1_genius_v2b/part4/render_part4.log`
  (Part 4 render timing)
- `~/MEMORY.md` (durable rules only; no credentials, keys, or personal data
  are copied into this pack)
