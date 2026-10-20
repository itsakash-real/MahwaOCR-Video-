# Chapter build workflow — wide/adaptive-mode video builds

This is the canonical, single-source-of-truth workflow for building or
continuing a chapter video on `video/wide-mode-v2` (or a branch made from it).
It exists because this is a multi-chapter job (chapter 1, then 2, then 3...)
that tends to run across one long continuation session, and the detailed
phase-by-phase steps fade from a session's working memory long before the
general house rules in `AGENTS.md` do.

**Read this file in FULL before starting a chapter build, and read it again
before starting each NEW chapter in the same session** — even if you already
built an earlier chapter a few messages ago in this same conversation. Do not
rely on memory of it from earlier in the conversation. This is the actual
fix for "the agent forgets the workflow between chapters": re-reading is a
required step per chapter, not a one-time thing at session start.

If you were invoked via the `/build-chapter` skill, the series name, chapter
number(s) and audio file were already parsed from what the user typed — use
those values everywhere below that says `<SERIES>` / `<N>` / the audio path.
If you were pasted this file directly instead, ask for (or infer from the
session) whichever of those three you don't already have before starting.

---

## Before anything else

- Follow `AGENTS.md` and this workflow exactly. Work only through the CLI
  (`python -m manhwa_ocr <command>`), never hand-edit plan/alignment JSON,
  never create or convert audio.
- **Whisper/align needs a Python with `pywhispercpp` installed.** Check which
  one on THIS machine has it (`python -c "import pywhispercpp"`, try the
  system python first) and use that one consistently for `video align`. Only
  set up a dedicated venv if no python on the machine has it. Don't assume a
  specific venv path from a previous machine/session.
- Work on branch `video/wide-mode-v2` (or a worktree of it, if this
  environment uses worktrees for isolation — check `git worktree list`
  before assuming one exists). Never touch `main`; never merge without an
  explicit go-ahead.
- Always pass `--session "<SERIES>"` explicitly on every command (never rely
  on "the active session" — the user may have switched it in the dashboard).
- Make a todo list first (this session's task list, not this file) and keep
  it updated as you move through the phases below.

## Phase 1 — Deep audio↔video mapping BEFORE building

- From the alignment: list every sentence, its time window, and its tagged
  panel(s). Confirm each tagged panel actually shows during its sentence
  window.
- List every panel in the chapter's first pages that never appears. For
  each, decide: credits/text-only/transition (skip is correct) or a real
  story beat the narration covers (must be added). Read the pairing sheets:
  if a sentence narrates two beats and the second beat has its own panel,
  that panel belongs in the sentence.
- Shot audit: histogram of shot durations (no shot under 3.0s), count
  same-panel punch-in splits, and count DEAD PANS — a pan_left/pan_right on
  a window spanning the panel's full width (or pan_up/pan_down on full
  height) renders as a frozen still while captions flip underneath, which
  reads as the picture "refreshing" every ~2s. There must be zero.
- Report the mapping findings before changing anything.

## Phase 2 — Pairing fixes (only if the mapping shows wrong/missing panels)

- Change ONLY the wrong sentences; leave correct ones untouched. The panel
  must depict the sentence's subject (right character, action, location).
  Never use text-only, bubble-only, blank, or credit panels. A sentence may
  hold 1-2 panels (two tagged panels show as a sequence or pair -- use this
  for two-beat sentences instead of re-showing one panel). Sequence rule:
  on a sentence of <=8s ONLY the first tagged panel displays (the second
  tag is silently unused), so put the single best panel first; a two-panel
  sequence needs a >8s sentence.
- Edit `.work/video/ch<N>/pairs.json` (its numbering = alignment sentence
  numbering -- verify against the segment text first), then
  `video pair --chapter <N> --apply`. After any pairing change you MUST
  re-run `video align`.

## Phase 3 — Plan, then audit every window against the panel pixels

- `video align` -> `video plan --force`. Then compare EVERY shot's window
  with the panel art. **Prefer the storyboard** (`video/chapter_N.
  storyboard_0*.png`, written by `video plan`) over extracting frames from a
  render for this -- it's the same crop data the renderer will use, and it's
  one image Read per ~18 shots instead of a multi-minute render:
  - The subject's FACE fully inside the window, with margin.
  - No text line sliced mid-line by a window edge -- if a line can't fit,
    exclude it ENTIRELY (edge lands in the gap between lines). Never
    half-show a face either; exclude it whole instead.
  - Burned-in captions cover the bottom ~12% of the window: important
    faces/text must end above ~88% of the window height.
  - Panels whose subject bleeds off the panel's own edge in the source art
    are faithful, not defects -- say so and move on.
- Fix bad windows ONLY via `video/chapter_<N>.fixes.json`. Mechanics
  (learned the hard way): a fixes crop is a FOCAL box, not the final window.
  `wide_crop` grows it and anchors the window at the focal's top
  (containment), so **window top = focal top** and the window height is
  capped by the panel's own legal window budget (narrow panels get a
  partial, figure-centred extension rather than the whole focal box -- see
  `wide_figure_risk` in the plan/QA if one doesn't fully fit). The box's
  pixel aspect must match its named shape within 2% (9:16=0.5625, 3:4=0.75,
  1:1=1.0, 4:3=1.333, 16:9=1.777) or the WHOLE file is refused and nothing
  applies. After writing fixes, re-plan and verify the windows actually
  landed where intended via the storyboard -- do not assume.
- Check `plan["wide_figure_risk"]` / `plan["wide_caption_risk"]` /
  `plan["wide_reviewer_focal_ignored"]` (also in the QA file) for anything
  flagged on a panel you're about to ship -- these are honest, non-silent
  signals, not failures, but worth a look at that panel's storyboard tile.

**Code issues:** if you hit a genuine code bug, stop, fix it on
`video/wide-mode-v2`, run the tests (`python -m pytest tests/ -q`), commit
and push, then continue the build (this pattern is pre-approved; merging to
`main` is not).

## Phase 4 — Gates, in this order

`video align` -> `video plan --force` -> `video verify` -> `video sync-audit`
(must PASS) -> `video approve`. If sync-audit fails, fix the cause (pairing
or `{same}` flags) and re-run the chain -- never force past it.

## Phase 5 — Render

- First confirm no other `manhwa_ocr`/ffmpeg process is alive (two renders
  on one output file corrupt it).
- **Render a 480p preview first** (`--preview`) for your own review passes
  below -- don't jump straight to a full-resolution render while you're
  still checking for defects. Only render at final resolution once the
  preview looks right.
- Render once at the resolution actually requested for delivery. Verify the
  result on disk with ffprobe (dimensions, duration, size) before saying
  it's done.

## Phase 6 — Deep review of the FINISHED render

Sampled spot-checks are NOT a pass.

- Extract the first AND last frame of EVERY shot and check all of them: face
  cuts, edge-sliced text, caption collisions, artifacts.
- Also sample mid-shot frames ~2s apart on long shots to prove the motion
  is alive (consecutive frames must visibly differ -- a frozen frame means
  a dead motion).
- Classify every flaw you find and its reason. Fix real defects (pairing ->
  align -> plan, or fixes.json -> plan, or code -> push) and rebuild.
  Report honestly what remains and why (e.g. source-panel bleeds).

## Delivery

Give the finished video under a FRESH filename (never reuse one -- a cached
player will show the old file) plus wherever the user asks it delivered, and
a report with: what changed (explicit list), the gate results, and what
residue remains.

## Multiple chapters in one request

If asked to build/continue several chapters in one go ("build chapters 2 and
3"), run Phases 1-6 **separately, in full, per chapter** -- do not skip
phases for the later chapters because they feel redundant, and re-read this
file again before starting chapter 2's Phase 1 even though you just finished
chapter 1's Phase 6 a moment ago.

---

*Standing context: pacing rules -- select don't cover, max shots =
floor(audio_seconds/3), min shot 3.0s, one tagged panel per sentence held
its full window if <=8s, >8s splits tagged 60-70% + one impactful filler or
second tagged panel, filler only with a named character + visible action +
visible face. Recap style/voice is whatever this series' own recap config
says -- check the session's config before assuming a specific tone.*
