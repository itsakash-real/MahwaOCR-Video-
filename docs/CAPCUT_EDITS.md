# CapCut-style edits + video-defect bugfixes

What was added on branch `editor/capcut-edits-bugfix`, the config knobs
that control it, and what was deliberately NOT added and why. The
editor rules in `EDITOR_GUIDE.md` still apply; this doc covers only the
CapCut layer and the three defects it fixes.

## Defects fixed

### 1. A panel held >3s read as a 0.5s cut/glitch

Root causes: Ken Burns eased over 2.6s then held + the renderer
returned one cached static frame once a shot "settled" + segments were
joined by stream copy with no crossfade + a long sentence split into
multiple shots with the SAME panel restarted the entrance.

Fixes:
- `ShotRenderer.is_static()` is now gated on `_can_freeze`: only a shot
  whose EVERY card is a hand-set `hold` (payoff beats) may return a
  frozen frame. Continuous-motion shots are re-drawn every frame, so a
  >3s panel can never read as a frozen cut.
- `plan.shot_frame_bounds()` gives every shot exact `(f0, f1)` frame
  bounds: contiguous, no 1-frame gap/overlap. Concat stays a hard cut
  by design — there is no crossfade, so no frame is ever blended
  across a cut.
- `plan.ensure_split_crop_change()`: a split must never leave two shots
  on the same panel with the same crop. The later shot is punched in
  8% (pan → push_in) with the reason logged in
  `plan["split_crop_changes"]`. `split_long_shot()` already punched in;
  this is the safety net for every other split path.

### 2. Same panel 8s with left-right swap repeats >3 times

Root causes: the planner avoided only the exact previous motion key, so
a previous `pan_left` did not exclude the `pan` group and it could emit
`pan_right, pan_left, pan_right`, each restarting the viewport `q`
from -1 → +1; plus the pair slide re-triggered on every pair.

Fixes:
- `_Designer.motion()` avoids the whole `pan` group after a pan: never
  pan twice in a row, never repeat the previous motion (beat-cue shots
  that force `push_in` count as the previous motion too).
- `split_long_shot()`: later pieces never restart a pan (→ `push_in`)
  and alternate motion (`push_in` ↔ `pull_out`); only the first piece
  arrives with the shot's transition, only the last piece carries its
  exit (previously every piece copied both).
- `plan.ensure_motion_variety()`: final pass over the built shot list —
  a split shot's last piece can still match the next shot's motion, so
  the later shot's first-card motion is flipped, logged in
  `plan["motion_changes"]`. Hand-set `hold` is never flipped.
- `pair_move` fires at most once per pair shot: `Card.pair_move_done(t)`
  / `ShotRenderer.pair_move_done(t)`; a shot with `pair_move_done`
  starts with the first card already in place, so a re-render/clip
  never replays the slide.

### 3. Cuts faces + paints colour on faces

Root causes: crops could cut faces at edges; `clean_bubbles()` painted
ellipses/rectangles over bubbles, and the median-colour fill could land
on a character face as a flat grey / peach / white patch.

Fix: bubble painting was REMOVED ENTIRELY. `clean_bubbles()` is now a
no-op that returns the panel unchanged (pixel-identical) -- there is no
flag to re-enable painting, and no painted patch can ever land on a
face again. Bubble DETECTION stays (`bubble_regions` / `bubble_boxes`),
because crop scoring, drop decisions and QA still need it.

What happens to a panel with a bubble now:
- Crop to exclude the bubble, if a clean crop exists. `verify.pick_card()`
  adds the `face_bubble` problem and crop choice prefers windows with
  `head_ok` that avoid face-overlapping bubbles (`bubble_overlaps_face()`
  / `face_bubble_boxes()`), alongside the existing layout-sensitive QA
  (`layout_crop_problems`, pair-safe auto-switch).
- Else DROP the panel and re-pair its time to a neighbour. That is the
  P1 mask-risk rule, now applied to EVERY bubble that would have been
  painted -- not just dominant ones over a head. `verify.py` drops a
  panel whose best crop still shows a bubble (`lettering` /
  `face_bubble` problem); head-cut-only panels still go to
  "needs eyes", not to dropped.
- Stale cache: the cleaned-panel disk cache key is versioned (v2), so
  old cache entries that contain painted panels are never served again.

## CapCut-style edits added

Tasteful, recap-appropriate. Everything is gated, tested, and off or
rare by default.

### Transitions — `frames.TRANSITIONS`

One `transition` per shot (how the shot arrives):
`cut` (default, 90%), `fade` (10%), plus `slide`, `zoom`, `whip`,
`glitch` — the last four ONLY on impact/reveal beats. `fade` fades the
previous shot out (`exit: fade_out`) and fades in; `whip` is a 0.25s
fast push (`whip_in` entrance); `glitch` hard-cuts in with a 0.2s RGB
split (`frames.glitch_split`: R shifts left, B shifts right, G stays —
channels separate, no pixels are hidden, so it never covers a face).
Glitch at most once per 60s (`GLITCH_MIN_GAP`), tracked in the planner.
Every transition is a pure function of `(shot, time)`: frame-exact, no
flicker by design. Split pieces after the first are continuations
(`transition: cut`), not new arrivals.

### Motion — continuous Ken Burns

Every shot moves for its whole length (P3); `hold` is hand-set payoff
only. No two consecutive shots share a first-card motion; never pan
twice in a row; pans are smooth single-direction drifts, never
swap-repeats. See `ensure_motion_variety()` above.

### Auto-captions — `manhwa_ocr/video/captions.py`

Generalised from the intro video's burned-in subtitles to chapter
videos. Plan carries `captions`: each shot's sentence split into 3–5
word chunks (`split_sentence_to_chunks`, evenly balanced so no chunk
is under 3 words), spread evenly across the shot. Render writes
`video/chapter_N.captions.ass` and burns it in with ffmpeg's
`subtitles` filter in a final pass — captions never touch the per-shot
segment cache, so editing captions never re-renders a shot. Style:
bottom centre (Alignment 2, bottom band, never over faces), key word
(longest word in the chunk) yellow, rest white, black outline.

- Config: `video.captions: true` (default ON for NEW builds).
- Old plans have no `captions` key and render without captions unless
  requested: `video captions --chapter N` adds captions to the plan +
  writes the `.ass`. The next render burns them in.
- A subtitles-filter failure keeps the un-captioned video and logs a
  warning — it never fails the render.
- Captions are FULL text; callouts stay keyword-only. The two never
  double up: a callout is one keyword on an impact/reveal peak, a
  caption is the sentence itself.

### Effects — shake, punch, flash, glitch, blur-in

Kept from P3 (shake, punch, flash, blur-in entrance) + `glitch`
(above). All gated to impact/reveal beats, never on every shot. No
effect covers a face (glitch only separates colour channels).

### Music — beat sync

`editor.snap_cuts_to_beats(shots, beats)`: interior cuts snap to the
nearest beat within ±0.15s (`BEAT_MAX_SHIFT`). The first shot still
starts at 0, the last still ends on the video duration, no shot gets
shorter than 0.2s, shots stay back-to-back. Beats come from
`video.beats` (list of seconds) or `music/beats.json`
(`{"beats": [...]}` from any beat-detection tool); no beats = no-op,
cuts stay exactly on their sentences. Ducking unchanged (sidechain,
0.22 under narration). No music file → render without music, logged.

### SFX — scratch, rumble, hit

P3's `whoosh` / `impact` / `ding` / `sting` plus, from the intro SFX
plan: `scratch` (twist reveal: "but / however / except / suddenly" +
flash), `rumble` (long reveal, shot >5s + flash), `hit` (impact
sentence — slams / crashes / explodes / strikes / smashes / blasts —
with no shake effect of its own; a shake already gets `impact`). All
gated, never on every shot. Missing files → warning, render without
those SFX, never fail, never invent audio. Files live in
`video.sfx_dir`: `whoosh.mp3`, `impact.mp3`, `ding.mp3`, `sting.mp3`,
`scratch.mp3`, `rumble.mp3`, `hit.mp3`.

### Filters / grade — subtle only

`video.grade: null` (default OFF) or `"subtle"`: contrast +5%,
saturation +5% (`frames.grade_subtle`). The art still looks like the
art. The blurred + darkened + vignetted background is unchanged.

## Config knobs (`manhwa-ocr.yaml`, `video:` section)

| Knob | Default | What it does |
|---|---|---|
| `captions` | `true` | Auto-captions on new builds (3–5 words, key word yellow) |
| `grade` | `null` (off) | `"subtle"` = contrast +5%, saturation +5% |
| `beats` | `null` (off) | Beat times (seconds) to snap cuts to, ±0.15s; or `music/beats.json` |
| `sfx_dir` | `null` (= no SFX) | Folder with whoosh/impact/ding/sting/scratch/rumble/hit.mp3 |

## What was deliberately NOT added, and why

- **Stickers / emojis / GIFs / meme overlays.** They fight the recap
  format: the narration carries the story, panels carry the pictures.
  Decorative overlays would cover art and faces and date the video.
- **Speed ramps / slow-mo / reverse.** The narration is timed to the
  pictures by whisper sync; changing playback speed breaks sync and
  makes the voice drift. Pacing comes from cuts and punch-ins, not
  from stretching time.
- **Auto-zoom / shake on every shot.** Motion is continuous but calm;
  effects fire only on impact/reveal beats. An effect on every shot
  stops reading as emphasis and starts reading as a glitch — which is
  the defect this branch fixes.
- **Crossfades / dissolves between every shot.** Joins are hard cuts
  by design (frame-exact, `shot_frame_bounds`). A dissolve on every
  cut would blend two panels for ~0.5s and read as the old "0.5s cut"
  defect. `fade` exists for time skips / scene changes only (10%).
- **Per-word captions / karaoke highlighting.** Captions are 3–5 word
  chunks: readable at a glance, no flashing word-by-word highlight
  competing with the panel. Key-word yellow gives the emphasis without
  the strobe.
- **Auto beat-detection.** No beat-detection dependency is bundled;
  beats come from `video.beats` or `music/beats.json`. Snapping without
  real beats would move cuts for no reason.
- **Face beautification / skin smoothing / colour pops on faces.**
  Anything that repaints pixels on a face risks the grey-oval defect
  all over again -- which is why bubble painting was removed entirely.
  Grade is global and subtle; faces are never painted.

## Tests

`tests/test_capcut_edits_bugfix.py` covers: defect 1 (long shot no
static freeze, no same-panel split without crop change, join frame
counts exact), defect 2 (no motion repeat, no consecutive pans,
pair_move once per pair), defect 3 (bubble painting removed: panel
returned pixel-identical, bubble panel with no clean crop dropped,
no painted patch anywhere), transitions (frame-exact,
no flicker), captions (3–5 words, key word yellow, bottom band),
beat sync (cut moved ≤0.15s), SFX/music missing-dir fallback.
