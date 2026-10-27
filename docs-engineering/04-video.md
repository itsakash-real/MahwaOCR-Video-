# 04 — Video System (the Genius system, current)

**What this stage does.** Turns a locked script + frozen audio + approved
panel cuts into a continuous, full-bleed, sentence-synced 1080p movie,
with every shot verified frame by frame before anything is delivered.

**This is the current video system.** It is the Genius Knight workflow
from 2026-10-26, written down exactly on 2026-10-27 in
`GENIUS_YESTERDAY_WORKFLOW_EXACT.md`, and consolidated into repo `main`
as the `video movie` framepipe mode (commit `ea90739`). It **replaces**
the older methods. It is **not** Phase A pillar `fast_render.py`, **not**
Infinite Titles `it_render.py` (zoom-only, per-sentence 506 px windows,
width-fit blur, text-as-subject gates), and **not** the intro's fixed
2 s/image rhythm.

**Why it replaced them (measured, 2026-10-27 deep review).** The rejected
Infinite Titles ch1–10 set, measured on rendered video (24 shots/chapter,
interior 25% vs 75% frame difference, 240 shots): 184/240 (77%) below 1.0
(still), 222/240 (93%) below 3.0; medians ch1 0.03, ch2 0.01, ch3 0.03,
ch4 0.01, ch5 0.03, ch6 0.02, ch7 0.04, ch8 1.17, ch9 0.03, ch10 0.02 —
against Genius Chapter 1 on the identical method: **median 20.24, 0/24
below 3.0**. Text windows were worst in ch9 (~17/24) and ch10 (~16/24).
Bitrate was 385–482 kbps against Genius 1,278–1,419 kbps. Root causes:
the run used its own `it_render.py` + `METHOD.md` (which explicitly
rejected the Genius rotation), motion was specified as invisible
(zoom-only 1.00→1.05), the gate checked clipping not change, the gate
subject was text not face, and `PASS_WITH_RESIDUE` at scale (ch8 74,
ch10 64, ch9 36) meant "0 FAILs" did not mean no face cuts. The clearest
proof: ch6 s28 at 121.9 s — bubble whole, blond face only entering at
the window foot, and the gate passed it because the bubble was whole.

## Inputs and outputs

| | |
|---|---|
| Input: script | Locked script (refined, or user-supplied exact script) |
| Input: audio | Frozen audio + whisper sentence timings (see `03-audio.md`) |
| Input: panels | Approved subject-window cuts only, originals in `cuts_orig/` |
| Intermediate | `mapping_chN_phaseB.md` (or chapter mapping file), `shots_chN_rebuild.json` |
| Intermediate | `video_chN_rebuild.mp4` in the chapter's own workdir |
| Intermediate | Gate files, re-verification sheets, `chN_rebuild_report.md` |
| Output (coordinator only) | Combined movie in `~/workspace/your_files/`, fresh filename |
| Output (delivery) | Drive folder "Infinite Titles" (see §Delivery) |

## Step 0 — Lock inputs, decide first

Do this **before** any build, and write it down. If any item cannot be
locked, stop — do not build around it.

1. **Script:** on-transcript, expanded to cover all important panels,
   no intro/outro, no meta, no finale language at chapter ends
   (join-safe). Sentences split canonically — recount sentences; a
   149 vs 150 recount gap was a real bug to resolve, not ignore.
   Chapter 1 run 1 (v1 script) mapped **198 spoken units** (quote-aware
   split, not 192) — the unit count comes from the actual split, and the
   split must be recorded, not assumed from a word count.
2. **Voice/audio:** frozen audio (see `03-audio.md`). **Video never
   changes audio to fix a picture problem.**
3. **Panels — approved cuts only:**
   - 900 px wide, **vertical cuts only**.
   - Original cuts preserved in `cuts_orig/`.
   - **If a cut cannot be used, STOP — never silently substitute** a
     pipeline panel, a re-detected panel, or a panelfit crop.
   - **Skip rule:** skip **only** blank, white-space-only, or
     bubble-text-only panels. Every other skip is a fix, not residue.
   - `panels.json` descriptions can be flat wrong — judge the single
     panel file before re-pairing, never re-pair on a description alone.
   - Pairing rule: fix **only** the wrong sentences; leave correct ones
     as-is; the chosen panel must depict the sentence's subject; avoid
     text/bubble-only, blank, or credit panels; use panel ids exactly as
     shown; keep the panel count per sentence unless clearly wrong.
4. **Renderer:** main framepipe only (`video movie`, commit `ea90739`
   or later). **Forbidden:** `it_render.py`, zoom-only method,
   per-sentence 506 px windows, width-fit blur as default.
   (Checkout note: the local checkout is on `fix/video-vertical-motion`
   and does not contain `framepipe.py` — see `README.md`. Use a `main`
   checkout for the framepipe CLI, or the per-chapter session render
   scripts that implement the same steps below. Do not claim the CLI
   was used when the session script was.)
5. **Motion:** Solo rotation (Step 3), very slow, hard cuts, frame 0
   settled. Zoom-only is a FAIL before render starts.

## Step 1 — Mapping (sentence by sentence)

1. Map **every sentence to exactly one panel**, in story order, from
   the transcript. No sentence left without an image, no image left
   without a sentence, except the skip rule above, with every skip
   reported with a reason.
2. Write the mapping file per chapter (`mapping_chN_phaseB.md`), with:
   panel id, subject note, and window position.
3. **Window rule:** **900×506 for zoom shots, 900×560 for slide shots**,
   positioned by the subject note or by viewing the panel directly.
   **The subject must stay WHOLE through the FULL motion path, not just
   the first frame.** This is the rule the old system broke: a face
   whole on frame 0 but cut at the motion extreme is a FAIL.
4. **Face + action first (during windowing):**
   - Every window holds the speaking/acting face **whole** through the
     full motion path (first, mid, last at motion extremes).
   - Face cut / chin / jaw / forehead / eye cut = **FAIL**, never
     "residue".
   - Text/bubble-only window for a face sentence = **FAIL**. Dialogue:
     face first, bubble only if both fit; if not, **face wins** and the
     bubble loss is disclosed.
   - Subject-window cut rule (from user cuts): keep only the subject
     window, roughly **51–78% of the original height**, drop empty white
     / SFX / bubble excess at top or bottom, keep faces and main action
     whole.
   - Panel-description trap: descriptions can be flat wrong — judge the
     panel file.
5. **Coverage audit:** every useful panel used, every skip reasoned.
   No substitution. (Chapter 1 run 1: 198/198 mapped, 0 order violations,
   0 bounds violations, 101 distinct panels, tall_flag 40, uncertain 8
   — sentences 69, 88, 89, 92, 93, 94, 133, 187 — recorded, not hidden.)
6. **Verify-drop substitutions:** if the plan drops a tagged panel
   (mask risk) and substitutes a neighbour, make the substitution
   official by re-pairing the sentence to the substitute panel in
   **both** `pairs.json` and `cues.json`, then re-run
   `pair --apply → align → plan → sync-audit`. Otherwise the sync-audit
   still scores the original tag at 0% coverage and FAILs.

## Step 2 — Shots

File: `shots_chN_rebuild.json` (or the chapter's shots file).

1. **One shot per sentence**, sentence-synced timing — **not** fixed
   2 s/image. (Fixed 2 s/image was intro-only.)
2. **Frames per shot = round(sentence duration × 30).**
   **Total frames = round(total audio seconds × 30) exactly.**
   Sentence durations come from **measured whisper timings**, not
   nominal paragraph windows (which drift up to 0.55 s at edges).
3. **Motion in runs:** consecutive same-source + same-window shots form
   **one run** (invisible hold, no cut inside a run). Motion is assigned
   per run, continuous across the run.
4. **Motion cycles across runs, in order:**
   `zoom_in → slide_up → slide_down → zoom_out → repeat`. Very slow only.
   - Zoom: **1.00 → 1.05** (zoom_in) or **1.05 → 1.00** (zoom_out),
     centre crop.
   - Slide: **115 px total travel**, on a **1920×1195 base**, cropped to
     1920×1080.
   - **Frame 0 fully settled** — no slide-in / fade-in / zoom-from-black
     at shot start. Every new image appears instantly at its settled
     composition on the hard cut, then very slow motion only.
   - **Hard cuts only.** No fades, no transitions, no captions, no title
     cards, no end screen.
   - Note on the older Solo rule: the presentation law (below) supersedes
     any panelfit traverse/drift treatment. Panels are **placed**, big
     and centred, not scrolled through.
5. **Dead-pan check (lesson):** plan-level "dead pan" is conservative —
   the renderer keeps full-width-window pans visibly moving. Prove
   frozen-ness on **rendered frames** before churning re-plans, and
   recount dead pans on the **final approved plan**, never an earlier
   one, because motion assignments shuffle between plans.

## Step 3 — Render

Per-chapter script: `frame_pipe_render_chN.py` (adapted from
`hook_intro_work2/frame_pipe_render.py`), or the repo `video movie`
framepipe CLI on `main` (see Step 0, item 4).

1. **PIL renders EVERY frame exactly once** → rawvideo → ffmpeg.
2. **ffmpeg:** `libx264`, preset `veryfast`, `crf 18`, `yuv420p`,
   `1920×1080`, `30 fps`, AAC audio `192k`, **NO `-shortest`**.
3. **Base image per run:** cut cropped `(0, y0, 900, y0+win_h)`, resized
   with **LANCZOS** to `1920×1080` (zoom) or `1920×1195` (slide).
4. **Each frame:** apply motion at progress
   `p = (run_offset + frame_in_shot) / (run_frames - 1)`, continuous
   across a run.
5. **Output:** `video_chN_rebuild.mp4` in the chapter's own workdir.
   **Workers never write to `~/workspace/your_files/`** — the coordinator
   copies only after re-verification.
6. **Measured render speed:** Chapter 1 v2 Part 4: 1,693 frames
   (56.4 s) in **756 s wall** on 2 CPUs, no GPU. Budget ~13× real time
   for a single render; parallel renders contend (see
   `05-full-build-workflow.md`).
7. **Storyboard tile caveat:** storyboard tiles render ~0.03–0.04 tighter
   at the top than the plan window. Near-edge content can look shaved
   on a tile while the exact window crop and the rendered frames keep it
   whole. Before redesigning a focal on a re-audit complaint, crop the
   plan window from the panel and judge that; rendered first/last frames
   are the final arbiter.

## Step 4 — Gates (in order, never `--force`)

Run `gates.py` (or the chapter's gate script) in this order. **Any
failure stops the chapter. It is not forced past.**

1. **Duration:** ffprobe: 1920×1080, 30 fps, duration = audio duration,
   frames = expected total.
2. **Sync:** never forced. Frame total, contiguity, no gaps/overlaps.
   A sync failure stops the chapter.
3. **Full-bleed:** mid-frame of **every** shot extracted; shape must be
   exactly **(1080, 1920, 3)**; near-white % measured; **FLAG if white
   >30%** (list white >15% too). No pillars, no floating, no scrolling,
   no white letterbox, no stretching (all windows exactly 16:9 unless a
   tall-rule alternate is explicitly logged).
4. **Transition dominance:** 10 fps small frames (320×180); the frame
   difference at every shot boundary must dominate its neighbours
   (**>2.5× neighbour max**), except **EXEMPT-HOLD** (same run) and
   **EXEMPT-REFRAME** (same source, new window). Any FLAG = fix, not
   residue.
5. **First/mid/last visibility:** first, middle, and last frame of
   **every** shot viewed directly — face whole, subject whole, no
   floating, no scrolling, no white space.
6. **Quality (measured from the rendered video, not the log):**
   bitrate = size×8/duration. Must be at or above the Genius range
   (**1,278–1,419 kbps**). A 1920×1080 file under ~1 Mbps is a quality
   FAIL even if the container says 1080p. Source panels are 900 px
   upscaled 2.13× — do not claim native 1080p sharpness, and do **not**
   fake 4K by upscaling to 3840×2160 (no new detail, breaks the
   framepipe spec). If crf 18 lands under the floor (as Chapter 1
   parts 2/3 did at ~1.10–1.14 Mbps), re-render at a lower crf
   (Chapter 1 fix: crf 14 / crf 16 re-renders) and re-gate — do not ship
   the soft version.
7. **Motion (measured):** sample at least **24 shots/chapter**, interior
   frame difference (25% vs 75% of shot). Median must be in the Genius
   range (Genius ch1 median **20.24** on this method), and **<10%** of
   sampled shots below 1.0 (still). If the median is **<3.0**, motion
   has failed — rebuild, do not ship.
8. **Text census:** count text-dominant shots in the sample. If **>25%**
   are text-dominant, the windows are wrong — re-window, do not ship.

## Step 5 — Re-verification (separate stage, after gates)

1. **Dedicated re-verification workers, one per chapter**, viewing
   first/mid/last of **every** shot again (not a sample for the final
   sign-off).
2. **Fixes are window moves / re-pairs / re-cuts ONLY. Audio untouched.**
   (Exception: a gate that proves an audio defect — then the audio fix
   follows `03-audio.md` §Step 4, paragraph-only, and the chapter
   re-gates.)
3. Fixed shots are re-rendered, then **fixed + neighbouring shots are
   re-checked**.
4. **Repeat to 0 FAILs.** Residues are listed openly in the chapter
   report (`chN_rebuild_report.md`), never hidden as "0 FAILs = no
   issues". A sample gate is mandatory: the proof chapter (Chapter 1)
   is rendered, checked on all lines above, and **approved by the user**
   (motion, faces, quality) **before** Chapter 2 starts — in a
   multi-chapter run, no chapter is installed and no movie is joined
   until **all** chapters pass all lines.
5. **Tall / 9:16 rule (user, 2026-10-27 16:58 IST):** if a sentence's
   whole context is tall (9:16) and cannot be shown in 16:9 even with
   sliding: (1) note/log it, (2) **still render it in 16:9** as the main
   version, (3) **also render an alternate in 3:4 or 9:16** (blur
   background, best fit), (4) **share BOTH 2–6 s clips** of that part
   for the user to decide, (5) **never halt the whole render** for one
   such part. Priority: **face first, then main background action;
   text/bubble secondary.**

## Step 6 — Placement law (applies to every frame)

Panels are **big, CENTRED, and scaled UP until they touch the top and
bottom walls of the 1920×1080 frame** (the sides may rest over the
panel's own blur where its shape leaves room). **Never floating** —
never shown small in the middle of the frame on a backdrop, touching no
edge. **No scrolling** — no traverses, no strip scrolls, no drifting
panels as the presentation; panels are placed, not scrolled through.
**Zero white space** — no white bands, gaps, or letterbox space in any
frame, verified on rendered frames before delivery. This is the same
standard as the Shorts edits. Verify on rendered frames: no shot may
show a panel floating with space on all sides, no shot's motion may
read as scrolling, and no frame may contain white space.

Remaining white, when it appears after this law is followed, is **not**
margins — it is speech-bubble strips on white sub-panel backgrounds
inside multi-beat crop windows, or pale source art. That fix is a
re-crop / slice-hint pass on the worst windows — a separate job, on the
user's call, not a reason to add a backdrop or pillar.

## Step 7 — Combined movie (coordinator only, after all pass)

1. **Stream-copy concat only** (`final_concat.txt` / `combine_list.txt`,
   absolute paths). No re-encode, no intro, no chapter breaks, no fades.
2. **Verify:** movie duration/frames = **exact sum** of chapter
   durations/frames; joins are hard cuts with no black/blank/duplicated
   frame.
3. Only then copy to `~/workspace/your_files/` with **fresh filenames**.
   **Never deliver partial chapters or a partial set.** (Genius delivery
   rule, user 2026-10-26.)
4. Deliver with residues listed openly (unfixable spans, tall-rule
   decisions pending user choice). Never report "0 FAILs" as "no issues".

## Step 8 — Delivery (Drive, not chat)

- **Never paste a video in chat as an attachment.**
- **Only once all is done and verified**, upload to the Drive folder
  **"Infinite Titles"**
  (`https://drive.google.com/drive/folders/1d5Kg_z6U-eMh13r1AOfJgGSCYmEpBJ95`).
- Contents: **video + transcript + recap script + audio ONLY**.
  Assets mean transcript and recap script and audio — **not**
  images/panels/cuts.
- Do **not** upload drafts. Do **not** upload before verification.

## Chapter 1 v2 work-split note (not the normal rule)

Chapter 1 v2 was built as a **6-part work split** (parts at paragraph
boundaries only, never mid-sentence, so bugs are fixable per phase),
then joined into **one** single video by stream-copy concat in order
1→6, no re-encode, no fades, no black/blank joins, verified as the
exact sum of segments. That split was a work organisation for Chapter 1
only. **Chapters 2–4 are single videos, not parts** (user order
2026-10-27 20:09 IST). Do not copy the 6-part split onto later chapters
unless the user orders it again.

## Genius delivered set (the quality benchmark)

Final delivered Genius chapters (parent-verified from disk): ch1 387.08 s,
ch2 375.07 s, ch3 398.17 s, ch4 468.67 s, ch5 387.43 s, ch6 374.40 s
(total 2,390.82 s, average 6:38); combined `Genius_Knight_Movie_1080p.mp4`
2,390.86 s (ffprobe, 2026-10-27), 413,835,579 bytes, stream-copy concat, no intro/breaks/
fades. Final files are the `*_movie.mp4` names; `*_rebuild_preview.mp4`
files are **not** final. Residues still owed a user decision at the time
of writing: ch3 shots 3+14 (tall panel), ch4 shot 49 (new cut needed from
the page strip — Shan's head sits above the approved cut), ch5 S086,
ch6 audio route (patched splice), ch2 disclosed non-failure (6
same-panel pairs).

## Failure modes and what to do

| Failure | Do this |
|---|---|
| Approved cut cannot be used | STOP, tell the user. Never substitute. |
| Gate fails (any gate) | Fix the window/pair/cut, re-render affected shot(s), re-check fixed + neighbours. Never `--force`. |
| Face cut at a motion extreme | Re-window so the face is whole through the full path, or re-pair. Face wins over bubble. |
| Bitrate under ~1 Mbps / under Genius floor | Lower crf and re-render, re-gate. Do not ship soft video. |
| Motion median <3.0 | Motion has failed — rebuild, do not ship. Check the rotation was actually applied (zoom-only is the usual cause). |
| Text-dominant shots >25% of sample | Re-window, do not ship. |
| Sync audit scores an original tag at 0% after a substitution | Re-pair in both `pairs.json` and `cues.json`, re-run `pair --apply → align → plan → sync-audit`. |
| A shot looks shaved on a storyboard tile | Crop the exact plan window from the panel and judge that; rendered first/last frames are the final arbiter. |
| A `[LOOP: ...]` bracket swallowed a sentence in cues | Scan cues for `\x00` / loop-question-shaped untagged sentences before TTS — that pseudo-sentence would otherwise be narrated aloud. |
| A gate report shows only a tail of output | Read the **full** output. A `tail -4` once hid totals of up to 26 flagged sentences behind a "2 flagged" summary. |

## References

- `~/workspace/ManhwaRecaps/genius-knight-fallen-race/work_movie/GENIUS_YESTERDAY_WORKFLOW_EXACT.md`
- `~/workspace/ManhwaRecaps/infinite-titles/REBUILD_CHECKLIST_NO_REPEAT.md`
- `~/workspace/ManhwaRecaps/infinite-titles/USER_ORDER_video_after_ch10.md`
- `~/workspace/ManhwaRecaps/infinite-titles/work_movie/review_2026-10-27/DEEP_REVIEW_10_VIDEOS.md`
  (deep review of the rejected ch1–10 set, with measurements)
