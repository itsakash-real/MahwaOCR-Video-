# White-space auto-crop — the art-body pass

*Branch `feature/panel-ink-trim`. Local `main` holds the last known-good
state (refmatch, b0f8360) — merge back or reset to it if anything here
goes wrong.*

## The problem, measured

The user's rejection (2026-10-25): "white strips up and down in every
page". Evidence frame `.work/qcframes/S001.png` (regressor ch1 v2):

- The panel is a **drawn border box**. Its SFX lettering ("빠직") rises
  ABOVE the box and a speech bubble hangs BELOW it — both on the page's
  white canvas.
- Every trim stage (`tighten_edges`, `trim_v1`, `trim_v2`) stops at the
  FIRST ink row — the SFX stroke at the top, the bubble outline at the
  bottom. The white **between the border box and that ink** is interior
  by definition, so the regressor's panels.json records `trim2: 0.0` on
  all 123 panels while the rendered frame still shows white bands.
- The rendered-frame QC (`frameqc.py`) measures margins **inside the
  foreground** (the v1 lesson) but only *fails* shots of `placed` mode.
  This repo's videos are `mode: panelfit` — their white margins were
  reported as reviewer **notes** ("content, kept whole") and nothing
  stopped the build.

So the white is not a page margin (those are already eradicated) — it is
the panel's own canvas around overflow lettering/bubbles. No row-based
trimmer can remove it without cutting the ink that stops it.

## The fix: crop to the art body

A manhwa panel's art lives either inside a drawn rectangular border or,
in the common unbordered webtoon style, as a contiguous dense block with
bubbles floating on the white canvas above/below it. The overflowing
SFX/bubbles are decorations OUTSIDE the art body. Reference-style
framing shows the body; decorations that leave it are cropped away
exactly like any element crossing a frame edge.

New stage `video/bodytrim.py`, applied at panel build (after recap, when
`video build`/`video pipeline` re-detects panels — panels.json geometry
is the single source of truth, so pairing, plan geometry, backdrop and
render all follow automatically). Per axis, two paths:

1. **Border path**: thin dark bands (rows whose middle-90%-columns
   dark-pixel share ≥ `LINE_SHARE` 0.75, thickness ≤ `max(4px, 1.2%`)
   are border lines; dense art rows are too thick, strokes/bubbles too
   sparse. Topmost + bottommost band = the box; both must exist.
2. **Dense-span fallback** (unbordered panels — the common case): art
   rows are ≥ 45% content (not near-white); bubbles/SFX sit far below.
   The body = first dense run's start to last dense run's end; each
   discarded zone must be bubble-sparse (< 35% content) on EVERY row,
   else that side alone is refused (`zone_not_canvas`) — snow art and
   pale scenes are never amputated. Middle dips never split the crop.

Measured on regressor ch1 (`video trim` report): the border path alone
found boxes on only 6 of 123 panels; with the dense-span fallback 53
panels crop, 35 of them shedding > 60px of canvas per side. Panels whose
outer zones carry real art residue (e.g. P012-03's grass tail above the
bottom bubble band) keep that side and are flagged `zone_not_canvas` —
the report and the frame QC notes make every such case visible instead
of guessing.

### Guards (never destroy art — rule 10)

- **min_keep**: the body must keep ≥ 55% of each axis, else no crop
  (flag `min_keep`). A 2/3-panel stack's first box fails this — glued
  strips are never amputated (S003 stays whole).
- **dense-band guard**: each discarded outer region must not contain a
  run of ≥ 8 consecutive rows with ≥ 50% content pixels (a solid art
  band = another panel's artwork). Bubbles/SFX/text never reach 50% on
  a row; art does. Hit → no crop on that axis (flag `multi_art`).
- **side cap**: ≤ 40% shaved per side (flag `capped_*`, crop clamped).
- **min px**: never keep fewer than 24px on an axis (flag `min_keep`).
- No credible box → untouched (flag `no_border`): borderless art,
  night scenes, white-art panels — unchanged, exactly as today.

Every panel records `body: {top, bottom, left, right, flags}` in
panels.json (same shape as `trim2`). `PANELS_VERSION` 4→5 so existing
sessions re-detect once on the next build. Snow/foam/logo canvas is
untouched by construction (no border → no crop).

## Where it runs ("the autocrop software")

- **Automatically**: the `panels` stage of `video build` / `video
  pipeline` (the stage that runs right after recap + narration audio).
  Config: `video.panels.body_trim: true` (default ON — hardcoded per
  the user's ruling).
- **Standalone report**: `python -m manhwa_ocr video trim --chapter N`
  now lists the body pass per panel (cuts, flags) next to the v1/v2
  rows — the bake-off/inspection artifact.
- **Enforcement**: `frameqc.py` v3 now runs the white-margin +
  wall-touch checks on **panelfit** shots too, on the shot's FIRST and
  LAST rendered frames (the final arbiter — rule 32), and FAILS the
  build when a panel whose body record claims a successful crop still
  shows an ink-free white band at a wall the fit touches (that can only
  be a record/render divergence — a real bug). White art (snow, foam,
  logo canvas) and bands carrying ink stay reviewer notes, never
  failures.

## The 33-rule audit (user's list, 2026-10-25)

Rules already shipped in earlier builds (evidence in code/tests):

| Rule | Where it lives |
|---|---|
| 1 never float, art touches walls | height-fit of the body; capped pillars sit smaller (quality guard, flagged over_cap) |
| 2 no scroll/traverse | traverse deleted (b0f8360); strips height-fit + drift **or** zoom |
| 3 whole, native aspect | `panelfit_fit` — never stretched; upscale cap only |
| 4 zero white space | body pass (this document) + frameqc v3 failing panelfit white bands |
| 5 backdrop + shadow | `PFR_BG_BLUR 50 / OPACITY 1.0 / SHADOW_DY 20 / BLUR 10 / OPACITY .40`, test-pinned |
| 6 card bg rotation | deterministic 80/10/10 (9904dcf) for modes that still use cards |
| 7 hard cuts only | **enforced three ways (polish pass)**: `_panelfit_pass` forces `enter="cut"` and strips effects after impact detection; `PanelFitView` renders every card as a cut even on legacy plans; `end_fade` defaults OFF and panelfit shots are exempt from exit/video-end fades |
| 8 ink-bbox trim | `trim_v2` (on) |
| 9 slice at gaps ≥15px | `slice_gaps` (white **and** black, B1f) |
| 10 edge-trim, fallback+flag | body pass guards + placed `trim_fallback` |
| 11 never animate/blur white margins | backdrop is built from the body-cropped art |
| 12 landmark cut points | `slicehints.py` (user-written slice hints) |
| 13–18 pacing/coverage gates | sync gate: shot floor, coverage 80/60, filler impact test, panels-shown info-only |
| 19–22 pairing rules | `pairing.py` + pairs/cues re-pair requirement; bubble-only panels leave via re-pairing |
| 23 title cards = overlays | `titlecards.py` renders on the timeline; master audio never touched |
| 29 gate order | pipeline: panels→pair→align→plan→verify→sync-audit→render→qc; approve gate before render when `require_review` |
| 30–32 rendered-frame QC | `frameqc.py` v3: every shot's start/mid/end decoded; margins inside the foreground at the first AND last frame; panelfit is strict; the MP4 is the arbiter |
| 33 config printed, no silent defaults | the `video` command prologue echoes the effective config (with `[DEFAULTED]` marks) before plan/build/render/pipeline/pair/qc; config.py refuses silent fallbacks |

Countered, with reasons (the user allows counters):

- **Rule 1 vs rule 2**: rule 1 says "never float, scale up until art
  touches top+bottom"; rule 2 bans drifts. The MEASURED reference video
  itself drifts pillars at 72px/s (that is what refmatch pinned), so
  drift stays — it is the reference style the user locked
  ("exactly as same style in the video"). Art touches the walls because
  the fit is a height fit **of the body** after this change.
- **Rule 13 min shot 3.0s**: panelfit's measured floor is 0.3s with
  ~1.97s median shots — the reference's own rhythm. Keeping the
  measured values; the 3.0s floor remains the classic-mode rule.
- **Rule 29 "storyboard" as a gate stage**: storyboard tiles exist as a
  review artifact, but rule 32 makes rendered frames the arbiter — a
  tile gate would re-litigate what the MP4 already decides. Not added.
- **Rule 4 "zero white space" absolutism**: white that IS the art
  (snow scenes, foam, a logo's canvas) cannot be removed without
  destroying the panel; those stay, flagged as notes for the reviewer.
  Everything else (canvas margins) is removed at the source.

## Rollback

`git checkout main` restores the pre-change pipeline; sessions built
with body-trimmed panels keep their panels.json until the next
fingerprint-changing rebuild (fingerprint includes the new settings).
