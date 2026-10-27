# 01 — Transcription System

**What this stage does.** Turns a published manhwa chapter's pages into a
verified, panel-by-panel transcript: every panel's OCR dialogue/narration
text plus a written description of each panel's situation (characters,
action, setting). The transcript is the source of truth for everything
downstream — recap, audio, and video mapping all cite it, and nothing
downstream may invent a fact that is not in it.

**Session home (Infinite Titles):** `~/workspace/ManhwaRecaps/infinite-titles/`

## Inputs and outputs

| | |
|---|---|
| Input | Chapter pages from AsuraScans (chapter reader pages) |
| Intermediate | Page images in `pages/chN/`, tiles in `tiles/` |
| Intermediate | Per-range part transcripts: `transcripts/chN_partM.md` |
| Output (merged) | `transcripts/chapter_N.md` |
| Output (verified) | `transcripts/chapter_N_verified.md` |
| Output (report) | `verification/chapter_N_verification.md` |
| Tool scripts (session) | `ingest_ch.py`, `tile_ch.py`, `merge_chN.py`, `gemini_strict.py`, `build_chN_verify_prompts.py` |

## Step 0 — Confirm the chapter exists, before any work

1. Check the live AsuraScans series page / reader that the chapter is
   published. Do not invent chapters. (Infinite Titles was verified as
   20 chapters, 1–20, no gaps, status Ongoing, latest = ch20, on
   2026-10-26.)
2. Work one chapter at a time, in pipeline order. Do not start a later
   chapter's transcription while an earlier chapter is half-written, except
   for the safe overlap below: the *next* chapter's ingest + tiling may run
   while the current chapter's audio is building, because ingest/tiling do
   not touch the current chapter's files.

## Step 1 — Ingest the chapter pages

1. Run the session ingest script (`ingest_ch.py` / `ingest_ch1.py` pattern)
   for the chapter. It downloads the chapter's page images into
   `pages/chN/`.
2. Record the page count. Example measured values (Infinite Titles):
   ch1 21 pages, ch2 25, ch3 17, ch4 12, ch5 21, ch6 16, ch7 18, ch8 17,
   ch9 19, ch10 25. Ingest wall time is small (14–26 s in the examples).
3. **Page 1 trap (AsuraScans):** the chapter page JSON embeds its first
   entry as a wide credits banner, **1532×1024**, followed by 900 px-wide
   strips. A naive regex over the HTML captures the whole JSON blob as one
   "URL". Extract image URLs with a strict `[a-f0-9]{6}\.webp` pattern, and
   treat entry #1 as the credits page, not a story panel.

## Step 2 — Tile the pages

1. Run the tiling script (`tile_ch.py`). It cuts the long page strips into
   numbered tiles, `000`, `001`, … in reading order, into `tiles/`.
2. Record the tile count. Examples: ch1 153 panels from 21 pages (tile
   counts vary by chapter: ch4 90 tiles, ch5 130, ch6 96, ch7 119, ch8 117,
   ch9 116, ch10 145, ch11 146, ch12 129, ch13 136, ch14 114, ch15 122,
   ch16 116, ch17 129, ch18 121, ch19 114, ch20 135).
3. Tiling wall time is small (22–35 s in the examples).
4. Tiles are the unit of transcription. A tile may contain one panel, part
   of a tall panel, or a panel tail. Boundary decisions (see Step 4) are
   made on tile evidence, not assumed from page numbers.

## Step 3 — Transcribe in fan-out (never one agent reading all tiles)

**This is a hard lesson, not a preference.** A single agent reading ~180
tiles (2000 px, 900 wide) blows its context limit (~524K tokens) and dies
mid-chapter leaving a partial file. Always fan out.

1. Split the chapter's tile range into **4 roughly equal ranges**, one
   worker per range. The exact split depends on the chapter's tile count.
   Examples from the run: ch4 `000–022, 023–045, 046–067, 068–089`;
   ch6 `000–023, 024–047, 048–071, 072–095`; ch10 `000–035, 036–071,
   072–107, 108–144`. For a ~180-tile chapter the pattern is
   `001–045, 046–091, 092–136, 137–end` — but compute the split from the
   chapter's actual tile count; do not copy one chapter's ranges onto
   another.
2. Each worker reads only its own tiles, in order, and writes one part
   file: `transcripts/chN_partM.md` (M = 1–4).
3. For each tile, the worker records, in order:
   - Panel number in merged numbering (`pPP-NN`: page PP, panel NN on
     that page). Numbering is assigned at merge time; part files use
     part-internal numbering that the coordinator remaps.
   - All visible text, exactly as printed, including lettering quirks
     (example: ch9 page16 panel 3 is quoted as printed `ATTHIS MEETING...`
     — a lettering quirk, not a typo to "fix").
   - A description of the situation: who is present, what they are doing,
     the setting, and any visual detail the recap will need (titles,
     levels, items, expressions).
   - Flags where the evidence is ambiguous: speaker unclear, bubble
     without a tail, text cut off at a panel edge, illegible text. Flags
     are preserved, not resolved by guessing. Example: ch20's transcript
     keeps `STONE STO…` + an illegibility note where a window is cut off
     at the edge; a later "correction" to `STONE STATUE` was rejected on
     tile evidence because tile045 has no window at all.
4. **Non-story items are recorded, not dropped silently:** credits banner,
   chapter title card, end credits, end banner, studio logo. They are
   counted separately from story panels (e.g. ch8: 117 tiles → 108 story
   panels + credits banner + title card + end credits + end banner).

## Step 4 — Merge (coordinator only)

1. The coordinator concatenates the four part files in tile order into
   `transcripts/chapter_N.md`, with **no gaps** in tile sections
   (e.g. ch17: 129 tile sections 000–128, no gaps).
2. **Renumber scenes/panels into merged numbering** and **dedupe
   boundaries**: a panel that spans two part ranges, or two tiles, is
   counted once. Examples of boundary calls the coordinator must make on
   tile evidence:
   - A tile that is only the tail of the previous panel (e.g. ch6 tile037
     is page06 panel 7's tail, not a panel; ch13 tile093 is p13-7's tail
     only) — fold it into that panel.
   - Two tiles that are one continuous panel with no gutter between them
     (e.g. ch9 tile023 fold: part-1 Panels 15+16 into page06 panel 1) —
     fold into one panel.
   - A genuine border, even a diagonal or hard border, keeps panels
     separate (e.g. ch14 tile102 diagonal border → p17-9 and p17-10 stay
     separate; a worker merge hedge on this was rejected).
   - A blank tile, a blank gradient, or a page tail sliver is recorded as
     such, not counted as a story panel.
3. Count and report: total tiles, story panels, non-story items. The
   count must reconcile: story panels + non-story items = the number of
   OK lines at verification (see Step 5).
4. The coordinator eye-checks contested boundaries on the tiles
   themselves. Do not stack a fix on a single worker's numbers — two
   measurement workers were materially wrong on individual landmarks in
   an earlier project, and the rule since is: self-measure contested
   panels.

## Step 5 — Gemini verification (sequential, ban-safe)

1. Build verification prompts per range with
   `build_chN_verify_prompts.py`, splitting the merged transcript into
   **4–7 chunks** (keep each call small; ch20 used 7 chunks because range 6
   was split to keep calls at or below 21 tiles).
2. Send the chunks to Gemini **strictly sequentially, one key at a time**,
   model `gemini-3.5-flash-lite`, key 1 first, via `gemini_strict.py`.
   Rotate to the next key **only** on a 429, with exponential backoff and
   a circuit breaker. **Never parallel-blast.** Stop on any 429 that
   backoff does not clear. Transient 503s get a single spaced retry each.
   The Infinite Titles run recorded 0×429 across all 20 chapters; that is
   the result of this discipline, not luck to be spent.
3. Gemini returns, per range, OK lines and proposed corrections. The
   coordinator reconciles:
   - **Headers overstate.** Gemini's range headers routinely overstate
     counts (seen in almost every chapter). Reconcile to the actual OK
     lines, not the headers.
   - **A correction is only applied if the coordinator confirms it on
     the tile.** Examples of rejections that must stay rejected:
     ch9: Gemini's "TO BE ANNOUNCED" completion of a date line was
     rejected — it is absent from the tile; the transcript gains only
     the visible line.
     ch15: a speaker re-attribution was rejected on tile eye-check
     (bubble tail points up, not down) — the "speaker unclear" flag
     stands.
     ch20: the `STONE STATUE` "correction" was rejected — the tile shows
     `STONE STO` cut off, and the next tile has no window.
   - Mislabelled non-story lines (a tile tail called a "credits banner",
     a bogus "page09 credits banner") are ignored, not counted.
4. Write the verified transcript as
   `transcripts/chapter_N_verified.md` — body-identical to the merged
   transcript plus a header note, with applied corrections only — and the
   report as `verification/chapter_N_verification.md`, recording:
   corrections applied (count), corrections rejected (with tile reason),
   missed items (count), and the reconciled OK-line count
   (story panels + non-story items).
5. **Target: 0 corrections, 0 missed** is normal for a clean chapter
   (most Infinite Titles chapters hit it). A non-zero count is not a
   failure by itself — it is a signal to check the tiles, not to accept
   Gemini's word.

## Step 6 — Hand-off to recap

The verified transcript is the only input the recap stage may use for
facts. The recap worker receives `transcripts/chapter_N_verified.md` in
full — not a digest, not a summary. See `02-recap.md`.

## Failure modes and what to do

| Failure | Do this |
|---|---|
| A transcription worker dies mid-range (context) | Split that range smaller, re-run only that range, re-merge. Never let a partial part file stand as the chapter. |
| Boundary dispute between two part files | Coordinator eye-checks the tiles; the tile evidence wins over either worker's numbering. |
| Gemini 429 | Back off exponentially, rotate key only on 429, stop if it does not clear. Never parallelise to "catch up". |
| Gemini proposes a correction with no tile evidence | Reject it, keep the transcript's reading + flag, record the rejection in the verification report. |
| A panel's speaker is genuinely unclear | Flag it `[UNCLEAR]` / "speaker unclear" and carry the flag forward. Recap paraphrases it neutrally and never names the speaker (see `02-recap.md`). |
| Tile count does not reconcile at merge | Do not proceed to verification. Re-count tiles, part files, and folds until story + non-story = OK lines. |

## References

- `~/workspace/ManhwaRecaps/infinite-titles/STATUS.md` — per-chapter
  page/tile/panel counts, boundary calls, verification outcomes for ch1–20.
- `~/workspace/ManhwaRecaps/infinite-titles/transcripts/` — merged and
  verified transcripts; `verification/` — verification reports.
- Session scripts: `ingest_ch.py`, `tile_ch.py`, `merge_chN.py`,
  `gemini_strict.py`, `build_chN_verify_prompts.py`.
- Standing rule: Gemini ban-safety — see `README.md`.
