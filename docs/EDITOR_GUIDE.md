# Editor guide — top-class editor reference (P0–P5)

What the editor does to every video, and the knobs that change it. This
doc is a reference, not a tutorial — the workflows are in `MANUAL_WAY.md`
(by hand), `AGENT_WAY.md` (prompts A–G), `HYBRID_WAY.md` (split).
CapCut-style edits + the video-defect bugfixes live in `CAPCUT_EDITS.md`.

## Pacing + motion (P3)

- **Impactful-only panel selection (user rules, 2026-10-19): time is
  scarce, panels are abundant — SELECT, don't cover.** There is no
  "every drawn panel must appear" pass: a panel no sentence picked is
  simply not shown. `panels_shown / total` in QA is INFO ONLY, never
  success — success is tagged coverage + min shot + impact pass.
  - Max shots = `floor(audio_seconds / 3)`.
  - Min shot = 3.0s, ever (except the renderer's 0.5s black tail).
    A shot under the floor is counted, never hidden, and FAILS the
    sync gate.
  - One tagged panel per sentence, held for the full window if the
    sentence is ≤8s. No filler, no split.
  - A sentence >8s splits into 2 shots, each ≥3s: tagged (~65%) +
    ONE impactful filler (~35%) OR a same-panel punch-in. A sentence
    >15s: at most 3 shots, each ≥3s. Shots per sentence are capped at
    `ceil(duration / 3)`, preferring ≤2.
  - A filler passes the impact test ONLY if ALL hold: named character
    + visible action/story beat (scene note), visible face (figure
    share ≥ FIGURE_MIN; not text-only, SFX-only, body fragment,
    back-of-head, or a spoiler for a later sentence), max 1 per
    sentence (2 if >15s), min 3s, never a repeated panel, never
    backwards story order. If no panel passes, the tagged panel holds.
  - Duplicate-tag sentences (tag = previous sentence's panel) HOLD
    the panel — no filler, no new shot.
  - Tagged coverage: ≥80% of a ≤8s sentence's window, ≥60% of a
    longer one's. The sync gate FAILS on any sentence under its
    threshold, any shot <3s, or any filler that fails the impact
    test (a failing filler counts as wrong-picture).
  - Pair second cards arrive exactly on their own words — no arrival
    floor (the old 0.6s floor made boundary pairs structurally late
    past the 0.5s gate).
- **Panel hold:** every panel shows ~3–4s (5–7s only if it carries
  readable text). No panel >8s without a crop change or chapter card.
  Shots >8s are split into (4, 8]s pieces, each with an 8%-per-piece
  punch-in crop change. Split pieces respect the `min_hold` floor.
- **Motion:** continuous Ken Burns on every shot — never a frozen frame.
  Planner never emits `hold`. Motions are push_in (100→115%), pull_out
  (118→100%), and 4-way pans, varied so the same motion never repeats.
  Hand-set `hold` survives only for deliberate payoff beats. Never pan
  twice in a row, never repeat the previous motion; split pieces
  alternate motion and only the first piece arrives with the shot's
  transition. See `CAPCUT_EDITS.md` (defects 1–2).
- **Transitions:** hard cuts by default (90%), fade 10%. `slide` / `zoom`
  / `whip` / `glitch` only on impact/reveal beats; glitch (0.2s RGB split)
  at most once per 60s. Joins are frame-exact, concat is a hard cut by
  design — no crossfade. See `CAPCUT_EDITS.md`.

## Callouts (P3)

One keyword callout per impact/reveal shot: rank, level, number,
time-skip, or place. ≤40 chars, ≤2 lines, bottom banner, yellow for the
single keyword, never over the top 2/3 of the frame (face avoidance).
Override one shot's callout text in `fixes.json`:

```json
{"version": 1, "callouts": [{"shot_id": "S007", "text": "SS-RANK"}]}
```

## Auto-captions (CapCut)

Plan `captions`: each sentence split into 3–5 word chunks, spread
evenly across the sentence's alignment window (whisper sentence
timings — never per-shot estimates, which gap during filler shots
and show tails early), burned in at render from
`video/chapter_N.captions.ass` (ffmpeg subtitles filter, final pass —
captions never touch the segment cache). Bottom centre in a dedicated
black bottom bar (bottom 8% of the frame; cards capped to the top
92%), key word (longest word) yellow, black outline, never over panel
art. Full text; callouts stay keyword-only, so the two never double
up.
`video.captions: true` by default for new builds; old plans render
without captions unless requested:

```bash
python -m manhwa_ocr video captions --chapter N --session "Your Series Name"
# -> adds captions to the plan + writes video/chapter_N.captions.ass
```

A subtitles-filter failure keeps the un-captioned video and logs a
warning, never fails the render.

## Card backgrounds — deterministic rotation (user rule, 2026-10-19)

Shot index `i` (0-based, plan order): `i % 10 == 4` -> solid black,
`i % 10 == 9` -> solid white, else auto-blur backdrop. 80% blur / 10%
black / 10% white, evenly spaced, deterministic (no randomness).
Background only replaces the backdrop behind the card; card unchanged,
centred, full size, colours never inverted. Recorded per shot as
`shot["background"]`. `fixes.json` shot background override still wins.
Chapter cards / end-screen / black tail are not shots: rotation does not
apply to them.

2s-refresh guard: no panel may read as refreshing every ~2s. Enforced by
continuous motion + no motion repeat + `ensure_split_crop_change` (no
same-panel split without a crop change). Regression test:
`tests/test_background_rotation.py`.

## Chapter cards (P3 + P5)

Videos ≥6 min get a card every ~6 min at a shot boundary: 1.75s,
full-screen dark overlay, auto title from the boundary sentence
(max 40 chars). Cards feed YouTube timestamps via
`chapter_cards_to_timestamps()`. Override a title in `fixes.json`:

```json
{"version": 1, "chapter_cards": [{"shot_id": "S012", "title": "The Exam That Kills"}]}
```

## SFX (P3)

Plan `sfx` events: whoosh on chapter cards, impact on shake, sting on
  flash, ding on system/status-window sentences. CapCut extras: scratch
  on twist reveals, rumble on long (>5s) reveals, hit on impact
  sentences with no shake of their own. Render mixes them from
`video.sfx_dir`. Files needed in that folder: `whoosh.mp3`,
`impact.mp3`, `ding.mp3`, `sting.mp3`, `scratch.mp3`, `rumble.mp3`,
`hit.mp3`. Missing dir/files → one warning,
renders without SFX, never fails. No SFX ships with the program.

## Music (P3)

`music: null` means: explicit path wins, else `music/bed.mp3` in the
series folder when present, else music-free. Default volume 0.22
(15–25% under narration); sidechain ducking lets it rise toward 60–80%
when no narration speaks. Fade in/out 0.5s (≥0.3s).

**Beat sync:** `video.beats` (list of seconds) or `music/beats.json`
(`{"beats": [...]}`) snaps interior cuts to the nearest beat within
±0.15s — first/last cuts never move, no shot under 0.2s. No beats =
no-op, cuts stay exactly on their sentences.

## Hook + open loops (P3)

- First 30 seconds: proof (the exact rank/number/beat the title
  promises) + stakes + one-line roadmap. No greeting, no logo bumper.
- Open a loop every 2–3 min (`[LOOP: the question]`). End on an open
  question/cliffhanger. Biggest payoff at ~2/3, not in the last 30s.
- `[HOOK]` / `[LOOP: ...]` are whole-line markers in the recap; they
  attach to the next sentence, are never spoken, never appear in
  `tts.txt`. `plan["hook"]` carries explicit-hook + open-loop count +
  a warning when missing (never blocks).

## Hook video + previously (30s each)

- `video hook --chapters 1-6 --audio audio/chapter_0.mp3 --script intro/intro.txt`
  → `video/chapter_0.mp4`. `video intro --auto` is the same build.
  `video previously --chapters 1-6 --audio audio/previously_7.mp3
  --script previously/previously_7.txt` → `video/chapter_prev7.mp4`.
- Hook structure is the same rule as above, compressed: 0–10s proof
  (`[HOOK]`), 10–20s stakes, 20–30s roadmap + `[LOOP: ...]`. Missing
  markers warn, never block. No chapter cards, at most one callout.
- Auto-pick: lexical overlap with panel OCR text first, face/impact
  pixel heuristics on top (portrait ≈ close-up, wide ≈ establishing,
  low-dialogue ≈ clean impact). Never text-only/credits, never a repeat,
  story order never backwards, tease ceiling keeps payoff panels out
  until the narration names the beat. Picks + reasons land in
  `intro/intro.auto.json` / `previously/previously_prev7.auto.json`.
- Previously: beats from the previous part's **recaps** (never
  transcripts), panels ONLY from the given chapters (no next-part leak
  by construction), preference for panels already shown in those
  chapters' videos. Threads used for beat selection are cut off at the
  previous part — later-chapter threads never steer it.
- Memory: `video/intro_memory.json` records used panels / beats / open
  loops / last previously run. Later picks avoid them unless the
  sentence is marked `[REUSE]` (or `--allow-reuse`). `series_memory.json`
  is read-only here.
- 30-second rule: target 30s, 20–45s via `video.hook` config or
  `--duration`. Over/under logs a warning; audio is never trimmed.

## Crop / face safety (P1 + P2)

- A face is never masked. Bubble painting was removed ENTIRELY:
  `clean_bubbles()` returns the panel unchanged (pixel-identical) --
  there is no flag to re-enable painting. Bubbles stay visible, so
  crops steer around them (bubble scoring); a panel whose best crop
  still shows a bubble is DROPPED and re-paired (the P1 mask-risk
  rule, now applied to every bubble). Mask-risk bubbles (dominant
  bubble overlapping the head) are dropped the same way. See
  `CAPCUT_EDITS.md` (defect 3).
- Crop QA checks every layout (single / pair / wide). A crop clean in
  single but not in pair is `layout_sensitive`; the planner auto-switches
  to the pair-safe shape (e.g. 4:3 → 3:4) and logs the switch in
  `crop_stats["layout_switches"]` + QA. No switch happens when the pair
  layout has no clean crop — that stays a reviewer decision.
- Visible erased-bubble patches are flagged via `patch_visibility`
  (texture-contrast ≥0.12) as a reviewer warning, never a drop reason.

## Sync (P2)

One gate code path (`evaluate_gate` / `gate_from_report`) shared by audit
and render. Cached audits are reused only under the current sync
settings. `{same}` across a story jump fails validation (name the
skipped panel, retag). Timing warnings ride with plan + QA and the
estimated-timing render refusal names the exact warning code + fix.
The gate fails on: wrong-picture share / five-second windows / late cuts
(as before) PLUS any shot under 3.0s, any sentence whose tagged panel
covers less than its threshold (80% for ≤8s, 60% for longer), and any
filler that fails the impact test. QA reports tagged coverage per
sentence, filler shots/seconds, min shot, and panels shown/total
(info only).

## Safety (P0)

No silent fallbacks: every estimated-timing path records a structured
warning (`code` / `message` / `fix`) surfaced in `video status` via
`timing_warnings`. Corrupt cues fail loudly (`load_cues_strict`) instead
of falling back to even timing. `pairs.json` / `cues.json` validation
fails the whole file with line-level errors. Alignment + plan are
deterministic (same inputs → identical JSON).

## Config knobs (`manhwa-ocr.yaml`, `video:` section)

| Knob | Default | What it does |
|---|---|---|
| `format` | `16:9` | `16:9` YouTube, `9:16` Shorts |
| `fps` | 30 | Frames per second |
| `crf` / `preset` | 20 / `veryfast` | Quality / speed |
| `min_hold` | 3.0s | No picture stays up less than this; also the min shot (sync gate fails any shot under 3.0s) |
| `min_shot` / `max_shot` / `target_shot` | legacy, unused by the planner | Pacing is now: max shots = floor(audio/3), min shot 3.0s, pieces (4, 8]s |
| `pair_ratio` | 0.3 | Share of even-timed shots shown as two cards |
| `effects` | `{shake: true, punch: false, flash: false, end_fade: true}` | Which impact effects are on |
| `music` | `null` (= `music/bed.mp3` if present) | Music bed path |
| `music_volume` | 0.22 | 15–25% under narration |
| `music_fade` | 0.5s | Fade in/out (≥0.3s) |
| `sfx_dir` | `null` (= no SFX) | Folder with whoosh/impact/ding/sting/scratch/rumble/hit.mp3 |
| `captions` | `true` | Auto-captions on new builds (3–5 words, key word yellow) |
| `grade` | `null` (off) | `"subtle"` = contrast +5%, saturation +5% |
| `beats` | `null` (off) | Beat times (seconds) to snap cuts to, ±0.15s; or `music/beats.json` |
| `crop.full_width` | `true` | Cards keep full width, slide vertical window |
| `mode: fullframe` | off | No cards/backdrop: every shot is a full-bleed 16:9 camera viewport (pan/zoom through the panel); see "Full-frame mode" below |
| `fullframe.max_upscale` | 2.4 | Viewport source floor = frame / this (800×450 px at 1080p); zoom depth caps at the floor |
| `fullframe.zoom_depth` | 1.18 | Target push_in/pull_out depth inside the window |
| `fullframe.pan_travel` | 1.0 | Pan window = viewport × (1 + this) along the pan axis |
| `mode: placed` | off | Panel Placement Rule: whole panel at native aspect, centred at the maximum fit-inside scale (touches top+bottom or left+right walls) over its own full-brightness blur, NO motion, hard cuts only; see "Placed mode" below |
| `placed.bg_blur` / `placed.bg_opacity` | 50 / 1.0 | Backdrop blur radius / opacity (full brightness, shared with panelfit) |
| `placed.shadow_dy` / `shadow_blur` / `shadow_opacity` | 20 / 10 / 0.40 | Foreground drop shadow (shared panelfit treatment) |
| `placed.title_cards` | `true` | Timeline cues become split-flap title cards (shared panelfit machinery) |
| `placed.captions` | `false` | Pinned off: a placed panel touches the bottom wall, so captions would sit on art |
| `endscreen` | `false` | Write `chapter_N.endscreen.json` + `.watch_next.txt` at plan time (opt-in; `video endscreen` writes them on demand regardless) |
| `whisper_model` | `base.en` | Use `small` for Hindi narration |
| `require_review` | `false` | If true, render refuses unless plan approved |
| `sync.gate_wrong` / `gate_window` / `gate_grace` | 0.05 / 0.25 / 0.2 | Sync-gate thresholds (plus: any shot <3s fails, any sentence under its tagged-coverage threshold fails, any filler failing the impact test counts as wrong-picture) |

## Panel-fit mode (`video.mode: panelfit`)

The MEASURED reference style, as a mode alongside classic/wide/adaptive
(existing modes untouched). It replicates one specific channel's edit,
measured shot-by-shot from their video (85 shots; see
`reference-video-measured-spec.md`). Where the older text descriptions
of that style disagreed with the measurements, the measurements govern
(user arbitration 2026-10-23: "copy the video exactly — no captions,
no shake/flash"). No cards, no cropping of ordinary panels, no
stretch — ever:

- **Composition.** Every shot's panel is shown WHOLE at its native
  aspect ratio: sharp, centred, height-fit, over the same panel
  cover-scaled with a heavy Gaussian blur (~50px) at FULL brightness
  (no dimming layer — the measured video has none). Panels at aspect
  (w/h) >= `panelfit.fill_aspect` (~1.6) instead fill the frame
  full-bleed; blur bands are never used. The blurred background is
  static within a shot. Placement anchors CENTRE (B1e): a cover
  crop's overflow splits equally on both sides. The foreground casts
  a drop shadow — black, offset (0, +20px), blur 10px, opacity 40%
  (`panelfit.shadow_*`) — layered between the backdrop and the panel;
  the backdrop itself stays at full brightness.
- **Pre-processing (Rule 6).** Panel build must run with
  `video.panels.trim_v2: true` (prep order: recap → trim/slice → pair/plan
  → compose; raw pages untouched). Assets are trimmed to their ink
  bounding box (pure-white or pure-black rows/columns are margins) and
  strips are sliced at any contiguous pure-white gutter **over 15px**
  (`TRIM2_GAP_MIN_PX`, tightened from 200 in B1e — the user's ruling:
  stacked panels are ALWAYS separate assets, sequenced with the
  narration beats, never scrolled through glued), so neither layer
  ever contains a white margin. Slicing renumbers panel ids in story
  order; re-pair with the geometric-overlap id-remap after a rebuild.
  `video plan` warns when a panelfit build's panels carry no `trim2`
  record. Cuts/slices are recorded per panel in `panels.json` (`trim2`).
- **Motion.** Zero static, all LINEAR and sub-pixel (LANCZOS source +
  float AFFINE sampling; ease curves and integer rounding are banned —
  they read as jerk). Narrow pillars (aspect < `panelfit.drift_aspect_max`,
  0.72) get a gentle vertical DRIFT at a constant SPEED of 3.7% of
  frame width per second (≈71 px/s at 1080p; the reference fixes the
  speed, travel varies), total travel clamped to 5-13% of frame
  height — at the cap the panel HOLDS, never reverses, never slows.
  Motion is planned over the whole SENTENCE: when a sentence splits
  into sub-shots on one panel, the sub-shots slice one continuous
  motion (position-exact at the seams, one direction throughout), and
  a panel reused by the next sentence keeps its drift direction.
  Direction otherwise alternates sentence by sentence. The foreground
  is oversized by travel + headroom so no panel edge ever shows.
  Wider pillars and full-bleed shots get a zoom-IN at a constant RATE
  (+5.0%/s; the total grows with shot length, capped at +50% —
  `panelfit.zoom_max_total` — where the scale holds), sliced across
  sub-shots the same way. There is no horizontal motion of any kind.
- **Strips traverse (B1e).** A panel at/above the strip threshold
  (height/width >= 1.6, `panels.TALL_RATIO`) no longer drift-caps: it
  is fitted by width and TRAVERSES — a deliberate linear slide from
  the art's top (anchored at the frame's top) to its bottom (anchored
  at the frame's bottom), arriving exactly at the sentence span's
  end, sliced across sub-shots like every other span motion.
- **Duo (B1e).** A sentence hinted `duo` (below) shows its two panels
  SIDE BY SIDE, each in a half-frame cell over their shared blur:
  the left panel slides slowly UP, the right slowly DOWN, at the
  drift speed with the usual oversize, each foreground clipped to
  its cell. If either panel lacks slide room in its cell, BOTH fall
  back to a slow zoom-out.
- **Motion hints (B1e).** The pairing surface may overrule the
  routing for one sentence: a cues sentence's `"motion"` (or a
  pairs.json object entry's) is one of `default`, `zoom_out` (opens
  enlarged, settles to the fit at the plain zoom rate — a reveal),
  `zoom_out_fast` (same at `panelfit.zoom_out_fast_rate`, 2× the
  impact rate — a blast), or `duo`. A list parallel to the
  sentence's panels gives each sequenced segment its own motion.
  Unknown values are refused; default routing still never zooms out.
- **Impact.** Shots flagged by `{impact}`/`{reveal}` markers (fallback:
  a VISUAL_ACTION sentence with an impact verb) zoom at +8.8%/s
  instead. There is NO screen shake and NO white flash in this mode —
  the measured reference uses neither, on any shot.
- **Captions.** None are burned in, ever, in this mode
  (`panelfit.captions: false` pins it; the reference carries no burned
  captions — platform CC only).
- **Rhythm.** The mode's own shot floor is `panelfit.min_shot` (0.3s,
  the measured minimum; the sync gate honours it for these plans) and
  sentences over `panelfit.split_above` (3.0s) split into two shots.
  Narration for this mode should be synthesized with
  `tts.gap_seconds = panelfit.tts_gap_seconds` (0.11s — the SETTING,
  chosen because per-sentence TTS edge padding adds ≈0.09s: the
  measured silence lands at ≈0.20s, on the reference's 0.214s median).
- **Title cards.** Timeline cues in the narration ("Age 4", "Week 6 of
  possession", "3 years later") become split-flap title cards: solid
  black, a small serif label over two beige flap tiles (zero-padded
  digits, block = 33% of frame height), one bold condensed ALL-CAPS
  line in Anton below (rendered ink ≈12.2% of frame height). A card
  starts at its cue sentence's aligned start and lasts
  clamp(cue span, 1.4, 2.1)s, consuming the plan time it overlaps;
  the sync audit counts card time as covering its cue. A card is a
  VISUAL overlay only (B1e): the render muxes the chapter narration
  as one continuous audio input, so the sentence's audio plays
  unbroken underneath the card — never paused, split or muted (a
  render-level test proves the tone under a card window survives at
  full level).
  See `video/titlecards.py`.
- **Sync.** Shots anchor to sentence boundaries via the alignment, as
  in every mode; keyframe endpoints are the shot boundaries.
- **Parallax** is deferred (build B11): the detector yields boxes, not
  character segmentations, so cut-outs aren't cheap yet.
- **QA.** `chapter_N.qa.json` carries `panelfit_static` (must be
  empty), `panelfit_hold` (sub-shots sitting in a travel-cap hold —
  info only), `panelfit_over_cap` (panels fitted at the upscale cap —
  shown smaller, never stretched), `panelfit_impact` (which shots got
  the fast zoom rate, and by which rule), `panelfit_title_cards`, and
  (B1e) `panelfit_traverse`, `panelfit_duo`, `panelfit_zoom_out`
  (which shots traverse, pair side by side, or took a zoom-out hint).
  A card breaking the composition invariants fails the build.
- **Storyboard** shows the composed frame at shot start and end, side
  by side. Landscape only; portrait formats stay classic.

Knobs (`video.panelfit.*`, defaults pinned in code): `bg_blur` 50,
`bg_opacity` 1.0, `fill_aspect` 1.6, `drift_aspect_max` 0.72,
`drift_speed` 0.0658, `drift_min_travel` 0.05, `drift_max_travel` 0.13,
`drift_headroom` 0.03, `zoom_rate` 0.05, `zoom_rate_impact` 0.088,
`zoom_out_fast_rate` 0.176, `shadow_dy` 20, `shadow_blur` 10,
`shadow_opacity` 0.40, `max_upscale` 2.4, `still_limit` 0.5,
`min_shot` 0.3, `split_above` 3.0, `split_min_piece` 1.2,
`captions` false, `title_cards` true, `tts_gap_seconds` 0.11.
## Full-frame mode (`video.mode: fullframe`)

No cards, no backdrop, no borders: every shot fills the 1920×1080 frame with a
16:9 camera viewport that pans or zooms through the panel. The planner still
solves each shot's crop exactly as in the other modes (focals, `fixes.json`
crops, bubble avoidance all apply); the crop then becomes the *window* and each
card gains a `viewport` — `{window, from, to}` in panel fractions — whose start
and end rectangles are exactly 16:9 and inside the window. Tall windows pan
vertically, wide windows pan horizontally, tight windows zoom
(`push_in`/`pull_out`), and a window that is already 16:9 holds. Pairs show
their two panels as sequential full-frame views with a hard cut at the second
panel's `at`. Captions overlay the art's bottom band (no caption bar), and the
storyboard draws each shot's start and end viewport frames side by side.

Every style parameter is a named constant, not a convention: the knobs live in
`video.fullframe.*` (defaults in `config.py`, mirrored by the `FF_*` constants
in `video/plan.py` — a test fails if the two drift apart). `max_upscale` is the
zoom guardrail: the smallest viewport a shot may use is the frame divided by
it (800×450 source px at 1080p by default), and zoom depth is capped so a
viewport never sources from less. The invariants are enforced, not hoped for:
`plan.validate()` rejects a plan whose viewports leave their window, and the
build QA gate fails any full-frame card whose viewport is not exactly 16:9 —
a plan that drifted back toward card-style output cannot reach render. The
plan records `fullframe_caption_risk`, `fullframe_figure_risk`,
`fullframe_reviewer_focal_ignored`, `fullframe_over_cap` and
`fullframe_static` shot-id lists in the QA file for review. Timing, panels and
the sync audit are untouched by the mode: a full-frame plan has exactly the
classic plan's shot skeleton.

## Placed mode (`video.mode: placed`)

The Panel Placement Rule (user, 2026-10-24), as a mode alongside
classic/wide/adaptive/panelfit/fullframe (existing modes untouched).
The user rejected panelfit's presentation ("you are just scrolling
the panels" — its traverses/drifts) and classic's floating cards, so
this mode keeps panelfit's composition and removes ALL motion:

- **Composition.** Every shot's panel (or B1f slice unit) is shown
  WHOLE at its native aspect ratio — never stretched, never cropped —
  sharp and CENTRED, scaled to the MAXIMUM size fitting inside the
  1920×1080 frame (fit-inside/contain). Tall and square panels touch
  the TOP and BOTTOM walls; panels wider than 16:9 touch the LEFT and
  RIGHT walls. There is deliberately NO upscale cap: a cap would show
  a small panel smaller than the maximum, i.e. floating — the defect
  this mode exists to eliminate. The remaining frame area is the
  panel's own cover-scaled blur (~50px) at FULL brightness (never
  dimmed, never white/black/empty), built by the same renderer
  machinery as panelfit, and the foreground casts the same drop
  shadow (black, offset (0, +20px), blur 10px, opacity 40%,
  `placed.shadow_*`) between backdrop and panel.
- **Motion.** NONE within a shot — no traverse, no drift, no pan, no
  zoom — and transitions are HARD CUTS only. Per-sentence motion
  hints are IGNORED in this mode (reported in the plan/QA as
  `placed_hints_ignored`, never an error, never applied). A placed
  shot's first frame is pixel-identical to its last.
- **Timing.** Not a new model: the classic planner's sentence→shot
  mapping, `min_hold` floor, split machinery and sync audit apply
  exactly as for classic; the plan artifact records `"mode": "placed"`
  and every card carries a `placed` spec (`{kind: placed, base_w,
  base_h, off_x, off_y, scale, touches}` with the motion fields pinned
  to zero — `plan.validate()` rejects a moving placed spec, and the
  build QA gate fails any card that is not at the exact maximum fit).
- **Pre-processing (Rule 6).** Shared with panelfit: panel build
  should run with `video.panels.trim_v2: true`, and B1f slice hints
  apply — each slice unit is placed on its own band dimensions.
  `video plan` warns when a placed build's panels carry no `trim2`
  record.
- **Title cards.** Shared with panelfit (`insert_title_cards`): a
  timeline-cue sentence becomes a split-flap card shot on the
  timeline, so the master audio is never cut. The plan/QA record
  them as `placed_title_cards`. Captions are pinned OFF in this mode
  (`placed.captions: false`, like panelfit — a placed panel touches
  the bottom wall by rule, so burned captions could only sit on the
  art; set `video.captions` + `placed.captions: true` to override
  deliberately).
- **Storyboard** shows the composed frame at shot start and end, side
  by side (identical, by construction). Landscape only; portrait
  formats stay classic.
- **Rendered-frame QC.** `video render` (and the pipeline's `qc`
  stage) pixel-check the finished MP4 per shot: white margins INSIDE
  the foreground, wall contact, and zero motion — the user's three
  rejections, measured on the delivered frames
  (`video/chapter_N.frameqc.json`; strict failures stop the delivery
  unless `--force`).

Knobs (`video.placed.*`, defaults pinned in code): `bg_blur` 50,
`bg_opacity` 1.0, `shadow_dy` 20, `shadow_blur` 10,
`shadow_opacity` 0.40, `title_cards` true.

## `fixes.json` overrides (all in one file, `video/chapter_N.fixes.json`)

```json
{"version": 1,
 "drop": [{"panel": "P016-06", "reason": "sound-effect lettering only"}],
 "face": [{"panel": "P017-02", "top": 0.12, "bottom": 0.46}],
 "crop": [{"panel": "P018-05", "shape": "3:4", "box": [0.0, 0.10, 0.6, 0.85]}],
 "shot": [{"panel": "P019-01", "background": "black", "enter": "fade_in"}],
 "chapter_cards": [{"shot_id": "S012", "title": "The Exam That Kills"}],
 "callouts": [{"shot_id": "S007", "text": "SS-RANK"}]}
```

Validated as a whole: one bad entry refuses the file. Unknown shot_id in
`chapter_cards` / `callouts` = logged warning in `override_warnings`,
never a failure. Re-run `video plan --force` after any edit, then
`video verify` again.

## `video status` editor signals (P5 `editor` dict)

`timing_warnings` (code + fix) · `mask_risk` count · `layout_switches`
count · `hook` (explicit yes/no + open-loop count) · `chapter_cards`
count · `callouts` count · `sfx` (events + files present/missing) ·
`music` (file + present/missing) · `override_warnings`.
