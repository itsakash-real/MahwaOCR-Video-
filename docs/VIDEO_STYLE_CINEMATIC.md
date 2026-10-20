# Cinematic video mode — research & design

## Why a second mode

Our classic mode shows panels as small floating cards (thin white border, centred
on a dark blurred backdrop). It reads as "a slideshow of thumbnails" — the art
never fills the screen. For high-quality sources (AsuraScans, ~900px wide and up)
this wastes the art and hurts retention: the viewer's eye has nowhere to land.

Two viral manhwa-recap videos were frame-analysed to extract a better grammar:

| Video | Views | Length | Mean shot | Median shot | < 2s | > 5s |
|---|---|---|---|---|---|---|
| "This Novel Broke the Internet & Now It's a Manhwa!" (`DUpaLD29WXw`) | 268K | 41:42 | 4.65s | 3.93s | 10% | 33% |
| "#1 UFC FIGHTER Reincarnates As A SLAVE…" (`1gcX-jXmJDM`, Frieren Manhwa) | 1.28M | 3:47:53 | 2.69s | 2.44s | 31% | 6% |

Method: `yt-dlp` (android player client) → `ffmpeg` frame extraction →
`select='gt(scene,0.3)'` cut detection → shot-length statistics; ~130 frames
visually inspected per video, plus 10fps burst captures around cuts and a
4fps motion study inside single shots.

## The viral grammar (both videos agree)

1. **Panels are LARGE — always.** Tall panels are shown full-frame-height,
   centred, edge-to-edge vertically. Wide/action panels go **full-bleed**,
   filling the entire 16:9 frame (cropped top/bottom). There are no small boxes.
2. **No cards, no borders, no caption bars, no black bars.** The art touches the
   frame edges. Nothing floats.
3. **Side pillars are luminous, never dark.** For tall panels the side fill is
   the panel itself scaled-to-cover + heavy blur, kept bright (often a soft
   colour wash sampled from the panel). No dark vignette, no 80%-black scrim.
   The pillars feel like an extension of the art, not a backdrop.
4. **Hard cuts only.** 10fps burst captures around cuts show zero crossfades,
   wipes, or transitions — every cut is instantaneous.
5. **Slow continuous zoom inside every shot.** 4fps motion studies show a
   ~5–8% push-in (occasionally pull-back) across each shot's duration.
   The image is never static.
6. **No burned-in subtitles.** Narration carries the story; the frame stays
   clean. (Our classic mode burns captions into a black bar.)
7. **Character name cards.** On a named character's first appearance, a large
   stylised name overlay sits in the lower third (white text, black outline,
   e.g. "Kim Jin-Ho") for ~2s, then leaves. No other text overlays.
8. **Multi-panel compositions for rhythm, not thumbnails.** Video 1 sometimes
   shows 2–3 panels side-by-side **full-bleed** (a triptych filling the frame),
   used at montage beats — never as small cards on a backdrop.
9. **Pacing follows energy.** The 1.28M-view video cuts every ~2.7s on average
   (action), the 268K one every ~4.7s (slower mystery build). Neither ever holds
   a static small card.

## Cinematic mode spec (`video.mode: cinematic`)

Classic stays the default; cinematic is opt-in per series via config.

### Layout (`video/cinematic.py`)

- `layout_for(aspect)`:
  - `tall` (aspect < 1.0): full-frame-height, centred horizontally.
  - `fullbleed` (aspect ≥ 1.35): cover the 16:9 frame (crop top/bottom around
    the focal region from the existing crop cache).
  - `square` (1.0–1.35): treated as `tall`.
- Pillars (tall/square): panel scaled to cover frame → heavy gaussian blur →
  brightness kept at ~0.9, **no vignette, no darkening**. A 24px feather blends
  pillar into panel edge.
- No white border, no card shadow, no caption bar, no black/white backgrounds.
- The 80/10/10 deterministic background rotation does not apply in cinematic
  mode (the references never cut to black/white).

### Motion

- Every shot gets a slow zoom: 1.00 → 1.06 (push-in) or 1.06 → 1.00 (pull-back),
  direction alternating per shot, ease-in-out. Wide/fullbleed shots may pan
  vertically instead (top → centre) when the crop has headroom.
- Cuts are hard. No entrance animations (no slide/whip/zoom-in cards).

### Pacing

- `min_hold: 2.0` (vs 3.0 classic), `target_shot: 2.7`, `max_shot: 5.0`.
- Sentences longer than 5.5s get **intra-sentence reframe cuts**: same panel,
  second crop region (the existing crop cache already stores alternates),
  each reframe ≥ 2.0s. This gives the fast-cut feel without breaking
  narration sync (the sync gate counts same-panel reframes as correct).
- No pair layouts; one panel per shot, always.

### Captions & overlays

- `captions_enabled: False` by default in cinematic mode (clean frame, like the
  references). Re-enable with `video.cinematic.captions: true` if needed.
- **Name cards**: on the first sentence that introduces a named character
  (character list from the series memory), overlay the name lower-third for
  the first 2.0s of that shot — big condensed white text, black outline.
  Dismissed automatically; never on later appearances.

### What stays

- Venv python for align, no hand-edited plan/alignment JSON, painting removed
  (drop + re-pair), sync-audit gate, spot-check review flow — all unchanged.
- Bubble/face masking lessons still apply: full-bleed crops must exclude
  dominant speech bubbles (crop to the focal region, never the mask).

## Config

```yaml
video:
  mode: cinematic        # classic (default) | cinematic
  cinematic:
    captions: false      # burn minimal captions (no black bar) if true
    name_cards: true     # character name overlays on first appearance
    reframe: true        # intra-sentence reframe cuts on long sentences
    zoom: 0.06           # push-in amount per shot (0 = static)
```

## Rollout

- Branch `video/cinematic-mode` (from main); classic mode untouched and default.
- Implementation: `manhwa_ocr/video/cinematic.py` (layout + luminous pillars +
  name cards + `CinematicShot` renderer), `video.mode` / `video.cinematic` config
  in `manhwa_ocr/config.py` + `manhwa-ocr.yaml`, plan integration in
  `manhwa_ocr/video/plan.py` (`_cinematic_pass`: cinematic flag, hard cuts,
  alternating zoom, reframe splits, name cards), renderer swap in
  `manhwa_ocr/video/render.py`, per-plan shot floor in
  `manhwa_ocr/video/syncaudit.py`, character names via series memory in
  `manhwa_ocr/video/build.py`, agent pointer in `docs/AGENT_WAY.md` Prompt C.
- Tests: `tests/test_cinematic.py` (16 tests: layout, pillars, name cards,
  zoom/reframe motion, plan pass, config, classic-unchanged). Full suite:
  670 passed, 1 skipped (1 pre-existing root-artifact failure deselected).
- Smoke-tested on real Monster's Streamer Star panels: tall panel renders
  full-height with luminous pillars + name card; wide panel full-bleed.
- Next: render one full chapter of Monster's Streamer Star in cinematic mode
  as the proof; compare side-by-side with classic before picking the default.
