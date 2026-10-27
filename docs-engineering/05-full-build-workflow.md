# 05 — Full Build Workflow

**What this document does.** Pulls stages 01–04 into one end-to-end
workflow: architecture, flows, who does what, how a video actually
renders, and how long each stage takes — measured, with ranges, never a
false exact finish.

## Architecture at a glance

```
                       ┌─────────────────────────────────┐
                       │  USER (go / stop words are law) │
                       │  Approves: scripts, proof       │
                       │  chapter, residues, delivery    │
                       └──────────────┬──────────────────┘
                                      │ orders, approvals
                       ┌──────────────▼──────────────────┐
                       │  COORDINATOR (parent agent)     │
                       │  Owns: inputs lock, fan-out,    │
                       │  merge, gates sign-off, concat, │
                       │  Drive delivery, residues list  │
                       └──────┬──────────────┬───────────┘
                 ┌─────────────┘              └─────────────┐
                 ▼                                          ▼
   ┌─────────────────────────┐                 ┌─────────────────────────┐
   │  PER-CHAPTER WORKERS    │                 │  RE-VERIFICATION        │
   │  (one per chapter, own │                 │  WORKERS (one per        │
   │   workdir, never share │                 │  chapter, separate from │
   │   .work/ or outputs)   │                 │  the builder)           │
   └─────────────────────────┘                 └─────────────────────────┘
```

**Concurrency rules that keep this architecture from corrupting itself:**

- One worker per chapter, one workdir per chapter. Never double-drive a
  session from two chats — duplicate runs collide on `.work/chN`.
- Workers never write to `~/workspace/your_files/`; the coordinator
  copies only after re-verification.
- The coordinator merges; workers never merge each other's ranges.
- Gemini is sequential across the whole system, never parallel, even
  when chapter workers run in parallel (see `01-transcription.md`).
- Renders run in parallel across chapters, but they share 2 CPUs — see
  §Timings. Parallelism saves wall time on mapping/gates/fixes, not on
  raw render CPU time.

## End-to-end flow

### Phase A — Transcription (per chapter, sequential across chapters)

```
Confirm chapter published
  → Ingest pages (pages/chN/, ~14–26 s)
  → Tile pages (tiles/, ~22–35 s)
  → Fan-out: 4 workers, 4 tile ranges → 4 part transcripts
  → Coordinator merge → transcripts/chapter_N.md (renumbered, deduped)
  → Gemini verification, 4–7 chunks, sequential, key 1, rotate only on 429
  → transcripts/chapter_N_verified.md + verification/chapter_N_verification.md
```

Safe overlap only: the next chapter's ingest + tiling may run during the
current chapter's audio build. Nothing else overlaps across chapters in
Phase A.

### Phase B — Recap and refinement (per chapter)

```
Full verified transcript
  → Recap draft (beats, story order, transcript-only facts, [UNCLEAR] neutral)
  → Pre-TTS checks (labels = 0, brackets = 0, pacing rules)
  → Gemini refinement, 2 chunks, sequential
  → Word-by-word diff; revert any meaning change / failure mode
  → refined/chapter_N_refined.md (originals preserved; _fixed/_pretrim alongside)
  → If over 480 s at measured pace: connective-tissue trim only, preserve pre-trim
```

### Phase C — Audio (per chapter)

```
Locked script
  → Split on blank lines → paragraphs
  → Line breaks → single spaces (do NOT collapse ...)
  → One Deepgram POST per paragraph, aura-2-odysseus-en @ native 1.3
  → Stitch with 0.25 s paragraph gaps → audio/chapter_N/audio.wav + manifest
  → Whisper completeness per paragraph (mandatory)
  → Sentence-sync recovery from whisper word timings (mandatory, test on 1 chapter)
  → WPM check: ≥90% of paragraphs within ±30% of chapter median (mandatory)
  → QA loop: text fix → re-voice only that paragraph → re-check
  → FREEZE audio (video never changes it)
```

### Phase D — Video (per chapter, then coordinated concat)

```
Lock inputs (script, frozen audio, approved cuts, renderer, motion) — decide first
  → Mapping: every sentence → one panel, window 900×506 / 900×560, face whole
     through FULL motion path → mapping file + coverage audit
  → Shots: one per sentence, frames = round(duration × 30), total = round(total × 30),
     motion in runs, cycle zoom_in → slide_up → slide_down → zoom_out, frame 0 settled
  → Render: PIL every frame once → rawvideo → ffmpeg libx264 veryfast crf 18,
     yuv420p, 1920×1080, 30 fps, AAC 192k, NO -shortest, LANCZOS
  → Gates in order: duration → sync (never forced) → full-bleed → transition
     dominance → first/mid/last visibility → quality → motion → text census
  → Re-verification (separate workers): first/mid/last of EVERY shot again
  → Fixes: window moves / re-pairs / re-cuts only, re-render, re-check fixed + neighbours
  → Repeat to 0 FAILs, residues listed openly
  → [Tall-rule parts: 16:9 main + 3:4/9:16 alternate, both 2–6 s clips to user]
  → Coordinator only: stream-copy concat, verify exact sum + hard-cut joins
  → Fresh filenames in your_files/ → Drive delivery (only after ALL verified)
```

### Phase E — Delivery

```
All chapters pass + movie passes
  → Copy with fresh filenames
  → Upload to Drive folder "Infinite Titles" (video + transcript + script + audio only)
  → Report: delivered files, residues, tall-rule decisions pending user choice
  → NEVER paste video in chat. NEVER upload drafts or partial sets.
```

## How a video renders (the mechanics, step by step)

This is the framepipe / `frame_pipe_render_chN.py` model, as specified in
`GENIUS_YESTERDAY_WORKFLOW_EXACT.md` §4:

1. **Inputs to the renderer:** a shots file (one shot per sentence, with
   source cut, window, motion, and frame count) and the frozen audio.
2. **Per run, one base image:** the cut is cropped to
   `(0, y0, 900, y0+win_h)` — `win_h` = 506 for zoom, 560 for slide —
   then resized with LANCZOS to `1920×1080` (zoom base) or `1920×1195`
   (slide base).
3. **Per frame, one PIL render:** for frame `f` in a run of `R` frames,
   progress `p = (run_offset + frame_in_shot) / (R - 1)`. Motion is
   applied at `p`, continuously across the run, so a run that spans
   several sentences has no visible step at sentence boundaries
   (invisible hold).
   - Zoom: centre crop at scale interpolating 1.00 ↔ 1.05.
   - Slide: crop window moving 115 px total across the 1920×1195 base.
4. **Frames are written exactly once** to a rawvideo pipe; ffmpeg
   encodes that pipe (`libx264 veryfast crf 18 yuv420p 1920×1080 30 fps`)
   and muxes the frozen AAC audio at 192k. No `-shortest`: the video
   length is exactly the frame count, and the frame count is exactly
   `round(total_audio_seconds × 30)`.
5. **Concat (movie only):** the coordinator concatenates chapter files
   with ffmpeg stream-copy (`-c copy`) from a list of absolute paths.
   No frame is re-encoded, no fade or transition is inserted, and the
   result's duration/frames must equal the exact sum of its parts.
6. **What rendering does NOT do:** it does not choose panels, fix
   windows, change audio, add captions, add title cards, or decide
   motion. All of those are decided in mapping/shots, before render.
   A render that "looks wrong" is almost always a mapping/window
   decision, not a renderer bug — fix the decision, re-render.

## Timings (measured, with ranges — never a false exact finish)

Hardware measured on: **no GPU, 2 CPUs, 7.7 GB RAM**. All timings below
are wall time on that machine, unless stated.

| Stage | Measured time | Notes |
|---|---|---|
| Ingest (one chapter) | ~14–26 s | Page download only |
| Tiling (one chapter) | ~22–35 s | Cut strips into tiles |
| Transcription fan-out + merge (one chapter) | Minutes to ~1 h (chapter-dependent) | 4 workers in parallel; coordinator merge + boundary eye-checks dominate |
| Gemini verification (one chapter) | 4–7 sequential calls, ~30 s for a verify pass in one example (ch1-1600 condense ~4m43 s, verify ~30 s — those are stage timings for that chapter's workflow, not a promise) | Sequential by design; a 429 backoff adds unbounded wait — never parallelise to compensate |
| Recap draft + trim | Chapter-dependent; first drafts often 1,700–2,100 words trimmed to band | Trimming connective tissue is the long part, not drafting |
| Gemini refinement | 2 sequential chunks | Plus word-by-word diff + reverts by the coordinator |
| Audio build (one chapter, per-sentence era) | ~3m45 s–4m32 s wall (examples: ch17 3m45 s, ch12-fixed 4m32 s) | Paragraph method (B) is 29 POSTs for ch1 v1; wall time scales with paragraph count, not word count alone |
| Audio checks (whisper + WPM) | Minutes per chapter | Whisper small.en; completeness + sync recovery are mandatory, not optional |
| Mapping (one chapter) | Part of the ~2 h/chapter Genius average | Sentence-by-sentence, with panel viewing |
| Render (one chapter, alone) | ~13× real time: 56.4 s video = 756 s wall (Part 4, measured) → a ~424 s chapter ≈ ~90 min alone | PIL frame-by-frame; this is CPU-bound, not I/O-bound |
| Render (set, parallel) | Genius 6 chapters: render ≈ 2 h total | Parallelism helps across chapters but each render slows as CPUs contend |
| Gates (one chapter) | Tens of minutes | Frame extraction + viewing first/mid/last of every shot |
| Re-verification (one chapter) | The dominant cost: Genius set verification ≈ 6 h of the ~12 h total | First/mid/last of EVERY shot, twice (gates + re-verification), plus fix/re-check loops |
| Combined movie (concat + verify) | Minutes | Stream-copy; verification is duration/frames sum + join checks |
| **Genius 6-chapter total** | **~12 hours** | ~2 h/chapter average, all stages included |
| **Infinite Titles ch1–10 (rejected set, for contrast)** | Not a benchmark — do not use it to estimate | That set used a different, rejected renderer and is not representative |

**How to estimate a new chapter.** Take the chapter's audio seconds,
multiply by ~13 for solo render wall time, then add mapping (30–60 min),
gates + re-verification (60–120 min for a ~200-shot chapter), and fix
loops (20–40 min per failed part). Report the result as a **range** with
start time, stage, and elapsed time — never a false exact finish.
Example: "Chapter 1, started 19:06, render of remaining parts ~30–40 min,
verification ~60 min, join + final check ~10 min → 1.5–2.5 h from now,
if no gate failure forces a re-render."

**Deadline rule.** If a user deadline (e.g. "complete by 12:00", "render
all 4 by 21:00") conflicts with the gate sequence above, the gates win.
Report the true verified status at the deadline: what rendered, what
passed, what failed, what is still open. Do not skip gates, do not ship
a soft video, and do not report a chapter as done when only its render
finished.

## System invariants (things that must always be true)

1. Audio duration = video duration (frames = round(duration × 30)).
2. One shot per sentence; every sentence mapped exactly once.
3. Every shot's subject is whole at first, mid, and last frames, at
   motion extremes.
4. Every boundary between runs is a hard cut that dominates its
   neighbours (>2.5×), except EXEMPT-HOLD and EXEMPT-REFRAME.
5. No frame contains a pillar, a floating panel, a scroll, or white
   letterbox space (except a logged tall-rule alternate, which is a
   separate file, not the main version).
6. Bitrate ≥ Genius range; container 1920×1080, 30 fps, crf 18 (or lower
   crf if that is what it takes to hold the floor).
7. Movie duration/frames = exact sum of chapter duration/frames.
8. Delivery set = video + transcript + script + audio, in Drive, after
   all verification, with residues listed openly.

## What a new agent must NOT do (the failure catalogue)

- Do not use `it_render.py`, `fast_render.py`, zoom-only motion, fixed
  2 s/image, width-fit blur, or text-as-subject gates. All were rejected
  with measured evidence.
- Do not substitute panels, force gates, or report "0 FAILs" as "no
  issues". See `04-video.md` §Failure modes.
- Do not re-voice a whole chapter, rewrite a whole script, or re-render
  a whole movie for a local fix. Fix the paragraph / shot / part, and
  re-check its neighbours.
- Do not read a whole back-catalogue for a new high-numbered chapter.
  Context ladder: chapter → synopsis / "previously" → thread/wiki →
  skim previous 1–2 chapters max. Never state unverified facts.
- Do not start a password reset, ask for raw credentials, or print a
  key. Use the connector / key-file routes documented in `03-audio.md`.
- Do not upload, share, or paste a video before verification. Do not
  share a Drive file with a person without telling the user who gets
  which access and waiting for their yes (Drive sharing rule).

## References

- All stage files in this pack (`01`–`04`) and their architecture
  companions.
- `~/workspace/ManhwaRecaps/genius-knight-fallen-race/work_movie/GENIUS_YESTERDAY_WORKFLOW_EXACT.md`
- `~/workspace/ManhwaRecaps/infinite-titles/REBUILD_CHECKLIST_NO_REPEAT.md`
- `~/workspace/ManhwaRecaps/infinite-titles/STATUS.md`
- `~/workspace/ManhwaRecaps/infinite-titles/work_movie/ch1_genius_v2b/part4/render_part4.log`
