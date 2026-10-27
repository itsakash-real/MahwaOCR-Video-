# Architecture — Video System

Companion to `04-video.md`. Design, data flows, and failure modes for
the Genius video system: mapping → shots → render → gates →
re-verification → concat → delivery.

## System context

```
Locked script + frozen audio + whisper sentence timings
    + approved cuts (cuts_orig/)
    │
    ▼
[Mapping] ──► mapping file + coverage audit
    │
    ▼
[Shots] ────► shots JSON (one shot per sentence, motion in runs)
    │
    ▼
[Render] ───► chapter video (PIL frames → rawvideo → ffmpeg)
    │
    ▼
[Gates] ────► pass / FAIL (never forced)
    │
    ▼
[Re-verification] ──► first/mid/last of EVERY shot, again
    │
    ▼
[Fix loop] ─► window / re-pair / re-cut → re-render → re-check fixed + neighbours
    │
    ▼ (coordinator only, after ALL chapters pass)
[Concat] ───► continuous movie (stream-copy, exact sum)
    │
    ▼
[Delivery] ─► Drive folder "Infinite Titles" (video + transcript + script + audio)
```

## Data-flow diagram (DFD, level 1)

```
   ┌──────────────────────┐     ┌──────────────────────┐
   │ Frozen audio         │     │ Approved cuts        │
   │ + whisper sentence   │     │ (900 px, vertical,   │
   │   timings            │     │  cuts_orig/)         │
   └──────────┬───────────┘     └──────────┬───────────┘
              │                            │
              ▼                            ▼
   ┌──────────────────────────────────────────────────────┐
   │ 1. MAPPING                                           │
   │    Every sentence → one panel, story order           │
   │    Window: 900×506 zoom / 900×560 slide              │
   │    Subject whole through FULL motion path            │
   │    Output: mapping file + coverage audit             │
   └──────────────────────┬───────────────────────────────┘
                          │ panel + window per sentence
                          ▼
   ┌──────────────────────────────────────────────────────┐
   │ 2. SHOTS                                             │
   │    Frames = round(duration × 30) per shot            │
   │    Total = round(total × 30)                         │
   │    Motion in runs; cycle zoom_in → slide_up →        │
   │    slide_down → zoom_out; frame 0 settled; hard cuts │
   │    Output: shots JSON                                │
   └──────────────────────┬───────────────────────────────┘
                          │ shots + runs
                          ▼
   ┌──────────────────────────────────────────────────────┐
   │ 3. RENDER (frame_pipe_render / video movie)          │
   │    Per run: crop (0,y0,900,y0+win_h), LANCZOS resize │
   │    to 1920×1080 (zoom) / 1920×1195 (slide)            │
   │    Per frame: PIL render once at p =                 │
   │    (run_offset + frame_in_shot) / (run_frames - 1)   │
   │    → rawvideo → libx264 veryfast crf 18 yuv420p      │
   │    1920×1080 30 fps + AAC 192k, NO -shortest          │
   │    Output: chapter video in chapter workdir          │
   └──────────────────────┬───────────────────────────────┘
                          │ rendered video
                          ▼
   ┌──────────────────────────────────────────────────────┐
   │ 4. GATES (in order)                                  │
   │    duration → sync → full-bleed → transition         │
   │    dominance → first/mid/last → quality → motion →   │
   │    text census                                       │
   └──────────────┬───────────────────────┬───────────────┘
                  │ PASS                  │ FAIL
                  ▼                       ▼
   ┌──────────────────────┐     ┌──────────────────────┐
   │ 5. RE-VERIFICATION   │     │ FIX LOOP             │
   │    Separate workers, │     │ Window / re-pair /   │
   │    first/mid/last    │     │ re-cut → re-render → │
   │    of EVERY shot     │     │ re-check fixed +     │
   └──────────┬───────────┘     │ neighbours → back    │
              │                   │ to Gates             │
              ▼                   └──────────────────────┘
   ┌──────────────────────┐
   │ 6. CONCAT (coordinator only)                        │
   │    Stream-copy, absolute paths, exact sum, hard cuts│
   └──────────┬───────────┘
              ▼
   ┌──────────────────────┐
   │ 7. DELIVERY (Drive, after all verified)              │
   └──────────────────────┘
```

Data stores: `cuts/` + `cuts_orig/`, mapping files, shots JSON,
`pairs.json` / `cues.json` (pairing state — see §Pairing state below),
gate JSON/logs, re-verification sheets, chapter reports
(`chN_rebuild_report.md`), chapter videos (workdir only until the
coordinator copies them), combined movie.

## Components

| Component | Responsibility | Failure it prevents |
|---|---|---|
| Mapping | Sentence → panel + window, coverage audit | Unmapped sentences; text-as-subject windows; face cut at motion extreme |
| Shots builder | Frame counts from measured timings, motion runs + cycle | Fixed-rhythm drift; run-boundary steps; settle-in/fade-in at shot start |
| Renderer (PIL → rawvideo → ffmpeg) | One PIL render per frame, LANCZOS, crf 18, no `-shortest` | Re-encode loss; duration mismatch; blur from wrong resampler |
| Gates | Ordered, measured checks from rendered video | Log-trust (a gate that passes on a claim, not on frames); forced sync |
| Re-verification workers | Independent first/mid/last viewing of every shot | Builder blindness (the builder passing its own work) |
| Fix loop | Window / re-pair / re-cut only, neighbour re-check | A fix that breaks the shot next to it, unnoticed |
| Coordinator concat | Stream-copy only, exact-sum verification | Re-encode quality loss; black/blank/duplicate joins; partial delivery |

## Key design decisions

1. **Decide first, render last.** Mapping and shots are decisions;
   rendering is execution. Every expensive failure in the rejected set
   was a decision failure (wrong window, text as subject, invisible
   motion) that rendering faithfully reproduced. The architecture
   therefore puts the most human attention before render, not after.
2. **Runs, not shots, are the motion unit.** Consecutive same-source +
   same-window shots are one run with continuous motion. This is what
   makes a multi-sentence panel read as a hold rather than a stutter
   of tiny cuts, and it is why transition dominance exempts
   EXEMPT-HOLD (same run) and EXEMPT-REFRAME (same source, new window).
   A gate that did not understand runs would flag every hold as a
   failed cut.
3. **Frame 0 settled is a presentation invariant, not a preference.**
   Every new image appears instantly at its settled composition on the
   hard cut, then moves very slowly. Any slide-in, fade-in, or
   zoom-from-black at shot start breaks the Genius look and is a FAIL.
   This is also why the placement law (big, centred, touching top and
   bottom, never floating, never scrolled) is checked on rendered
   frames, not on plan windows — plan windows lie by ~0.03–0.04 at the
   top on storyboard tiles.
4. **Gates measure rendered video, never logs.** Duration, bitrate,
   motion, white %, and face visibility are all computed from the
   output file's frames. A renderer log that says "done" proves nothing
   about what the frames contain — the rejected set's gates passed on
   claims while faces were cut and motion was still.
5. **Re-verification is a separate stage with separate workers.**
   The builder does not sign off its own chapter. Re-verification
   views first/mid/last of every shot again, and its output (sheets,
   checklists, reports) is the sign-off record, not the gate log.
6. **Fixes are local and neighbour-aware.** A window move, re-pair, or
   re-cut changes one shot and can change the transition into and out
   of it. The fix loop therefore re-checks fixed **and neighbouring**
   shots. A fix that is not neighbour-checked is not a completed fix.
7. **Concat is stream-copy because re-encoding is loss.** Chapters are
   already encoded at crf 18 from 900 px sources. Re-encoding the
   movie would soften every frame again for no benefit. The exact-sum
   check (duration/frames = sum of parts) is what proves the concat
   did not drop, duplicate, or pad a frame.
8. **Tall panels are a dual-output problem, not a blocking problem.**
   A 9:16 context that cannot fit 16:9 even with sliding does not stop
   the render. It produces two outputs (16:9 main + 3:4/9:16 alternate,
   blur background, best fit) and a user decision from 2–6 s clips of
   each. The architecture never lets one panel's aspect ratio hold a
   whole chapter hostage, and never lets the alternate silently replace
   the main version.

## Pairing state (`pairs.json` / `cues.json`)

Pairing (sentence → panel) is stored in `pairs.json` (chosen panel per
sentence, green = chosen, grey = neighbours in story order) and
`cues.json` (timing/cue state). These two files must agree. The known
failure: a plan drops a tagged panel and substitutes a neighbour, but
only one file is updated — the sync-audit then scores the original tag
at 0% and FAILs. The fix is procedural, not clever: re-pair the sentence
to the substitute in **both** files, then re-run
`pair --apply → align → plan → sync-audit` in that order. Any agent
that edits one file without the other has broken the pairing state.

## Invariants

- One shot per sentence; frames per shot = round(duration × 30);
  total = round(total audio × 30).
- Motion cycle order is fixed: zoom_in → slide_up → slide_down →
  zoom_out, repeating, very slow, zoom 1.00↔1.05, slide 115 px on
  1920×1195.
- Every shot boundary is a hard cut; every run boundary dominates its
  neighbours (>2.5×) unless EXEMPT-HOLD / EXEMPT-REFRAME.
- Every frame is 1920×1080, full-bleed, no pillar, no floating, no
  scroll, no white letterbox (main version).
- Bitrate ≥ Genius range (1,278–1,419 kbps); under ~1 Mbps = FAIL.
- Movie = exact sum of chapters, hard-cut joins, no black/blank/
  duplicate frame, fresh filename.
- No chapter is installed, no movie is joined, until all chapters pass.

## Performance model

Rendering is CPU-bound PIL work: ~13× real time solo on 2 CPUs
(56.4 s → 756 s wall, measured). Parallel chapter renders share the
same 2 CPUs, so total render CPU time does not fall with parallelism —
parallelism saves wall time on mapping, gates, and fix loops, and lets
a bug in one chapter be fixed while another renders. Verification is
human/frame-viewing bound, not CPU bound, and dominates total time
(Genius: ~6 h of ~12 h). Any estimate that counts only render time
underestimates a chapter by roughly 2×.

## Failure modes (design view)

| Failure | Design response |
|---|---|
| Approved cut unusable | STOP, tell user. No substitution path exists in the design — deliberately. |
| Gate FAIL | Fix loop, never `--force`. Forcing a gate converts a detectable defect into a shipped defect. |
| Face cut at motion extreme | Re-window for the full path, or re-pair. Face wins over bubble. |
| Motion median <3.0 | Rebuild. Usual cause: zoom-only rotation, or a plan that never applied the cycle. |
| Bitrate under floor | Lower crf, re-render, re-gate. Never ship soft video to hold a deadline. |
| Text-dominant sample >25% | Re-window. The windows are wrong, not the sample. |
| Fix breaks a neighbour | Neighbour re-check in the fix loop; repeat to 0 FAILs. |
| Concat drops/duplicates a frame | Exact-sum verification fails → do not deliver; re-concat from the chapter files, never by re-encoding. |
| Tall panel blocks a chapter | Dual output + user clips; never halt the render. |

## References

- `04-video.md` (procedure)
- `~/workspace/ManhwaRecaps/genius-knight-fallen-race/work_movie/GENIUS_YESTERDAY_WORKFLOW_EXACT.md`
- `~/workspace/ManhwaRecaps/infinite-titles/REBUILD_CHECKLIST_NO_REPEAT.md`
- `~/workspace/ManhwaRecaps/infinite-titles/work_movie/review_2026-10-27/DEEP_REVIEW_10_VIDEOS.md`
