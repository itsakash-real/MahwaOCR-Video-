# Cinematic video mode — research & design (v2)

## Why a second mode

Our classic mode shows panels as small floating cards (thin white border,
centred on a dark blurred backdrop). It reads as "a slideshow of thumbnails"
— the art never fills the screen. For high-quality sources (AsuraScans,
~900px wide and up) this wastes the art and hurts retention.

## Research

### Pass 1 (2026-10-20, automated frame analysis)

Two viral manhwa-recap videos were frame-analysed (yt-dlp with
`--extractor-args "youtube:player_client=android"` — the default client is
bot-blocked; `--download-sections` fails on these videos):

| Video | Views | Length | Mean shot | Median shot |
|---|---|---|---|---|
| "This Novel Broke the Internet & Now It's a Manhwa!" (`DUpaLD29WXw`) | 268K | 41:42 | 4.65s | 3.93s |
| "#1 UFC FIGHTER Reincarnates As A SLAVE…" (`1gcX-jXmJDM`, Frieren Manhwa) | 1.28M | 3:47:53 | 2.69s | 2.44s |

### Pass 2 (2026-10-20, browser-agent deep analysis of the Frieren video)

A detailed browser-agent pass (12-section report + 3 reference screenshots)
corrected pass 1 on the points that matter:

- **Cutting is faster than pass 1 measured**: first 150s sampled properly —
  **mean 1.87s, median 1.77s**, shortest ~0.33s, longest ~5.2s. Pace is steady,
  not tied to scene energy.
- **Side pillars are DARKER than the panel** (pass 1 guessed luminous/bright).
  Screenshots confirm: blurred enlarged copy of the art, dimmed to roughly
  half brightness. No borders, no rounded corners, no letterbox bars.
- **Name caption**: "Kim Jin-Ho" at 0:00–0:02 — large, bold, white with dark
  outline, centred lower third, quick pop/fade in.
- **Emphasis callouts**: large white outlined keyword (e.g. "172?") in the
  **upper right**, held briefly over the art.
- **Art-embedded SFX** ("DASH", "SIGH", "WHIN") stays in the art — never
  re-created as overlays.
- **Multi-panel**: two tall panels side by side on the blurred background
  (0:24.7); a wide close-up over a blurred card (0:17.5).
- **Black flash** (~1 frame at 0:10.5) — the only section break; no chapter
  text cards, no stings.
- **No subtitles, no channel branding, no grade/vignette/grain.** Bright
  scenes stay bright; dark scenes stay dark.
- **Audio**: loud and continuous (RMS ~0.07), no silences longer than 0.25s.
- **Hook**: cold open straight into action + name caption, no title card.
- **Ending**: YouTube's own end-screen cards over the art — nothing burned in.

## The viral grammar (v2, corrected)

1. **Full-bleed action art edge-to-edge is the default frame.**
2. **Tall panels sit centred on a blurred, enlarged, DARKENED copy of
   themselves** (~0.55 brightness). Sides clearly darker than the panel.
3. **Hard cuts only**, with at most a brief black flash at section breaks.
4. **Slow push-in / pan over still art** inside each shot (visually evident;
   not measured frame-by-frame).
5. **No burned-in subtitles.** No channel branding.
6. **Character name caption** on introduction: large bold white, dark outline,
   lower third, ~2s.
7. **Emphasis callouts**: large white outlined keyword, upper right, brief.
8. **SFX text stays in the art.**
9. **Occasional side-by-side tall panels** on the blurred background.
10. **Fast uniform cutting**: median ~1.8s.

## Cinematic mode spec (`video.mode: cinematic`)

Classic stays the default; cinematic is opt-in per series via config.

### Layout (`video/cinematic.py`)

- `layout_for(aspect)`:
  - `tall` (aspect < 1.35): full-frame-height, centred horizontally.
  - `fullbleed` (aspect ≥ 1.35): cover the 16:9 frame (crop top/bottom).
  - `pair`: two tall panels side by side with a gap, on the darkened pillars.
- Pillars: panel scaled to cover frame → heavy gaussian blur → dimmed to
  0.55 brightness. A 24px feather blends pillar into panel edge.
- No white border, no card shadow, no caption bar, no black/white backgrounds.
- The 80/10/10 deterministic background rotation does not apply in cinematic
  mode (the reference never cuts to black/white).

### Motion

- Every shot gets a slow zoom: 1.00 → 1.06 (push-in) or 1.06 → 1.00
  (pull-back), direction alternating per shot, ease-in-out. Hard cuts only.

### Pacing

- `min_hold: 1.5` (reference median 1.77s), reframe splits on shots > 3.2s
  (pieces ~1.8–2.5s each, ≥ 1.5s). Same-panel reframes don't break narration
  sync (the sync gate counts them as correct).
- No pair layouts from the planner by default (renderer supports them);
  one panel per shot.

### Captions & overlays

- `captions_enabled: False` by default (clean frame, like the reference).
  Re-enable with `video.cinematic.captions: true` if needed.
- **Name cards**: on the first sentence that introduces a named character
  (character list from the series memory), overlay the name lower-third for
  ~2s — big bold white text, dark outline.
- **Callouts**: on impact/reveal sentences carrying a keyword (rank, level,
  number, time skip, place — reused from the classic extractor), pop a large
  white outlined keyword upper-right for ~1.5s.
- **Black flash**: the first shot opens with a 0.12s flash-in from black.

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
    name_cards: true     # lower-third name overlay on a character's first appearance
    callouts: true       # upper-right emphasis keyword on impact/reveal beats
    reframe: true        # intra-sentence reframe cuts (pieces ~1.8-2.5s)
    zoom: 0.06           # slow push-in amount per shot (0 = static)
    min_hold: 1.5        # shot floor in cinematic mode (reference median: 1.77s)
```

## Rollout

- Branch `video/cinematic-mode` (from main); classic mode untouched and default.
- v1 (superseded): luminous pillars, 2.0s floor — rejected by the user; the
  browser-agent report showed pillars are darkened and cutting is faster.
- v2 (current): darkened pillars (0.55), 1.5s floor, reframe_at 3.2s,
  callouts, black flash-in, pair layout support in the renderer.
- Implementation: `manhwa_ocr/video/cinematic.py` (layout + dark pillars +
  name cards + callouts + `CinematicShot`), `video.mode` / `video.cinematic`
  config in `manhwa_ocr/config.py` + `manhwa-ocr.yaml`, plan integration in
  `manhwa_ocr/video/plan.py` (`_cinematic_pass`), renderer swap in
  `manhwa_ocr/video/render.py`, per-plan shot floor in
  `manhwa_ocr/video/syncaudit.py`, character names via series memory in
  `manhwa_ocr/video/build.py`, agent pointer in `docs/AGENT_WAY.md` Prompt C.
- Tests: `tests/test_cinematic.py` (21 tests). Full suite: 670+21 new pass.
- Next: render one full chapter of Monster's Streamer Star in cinematic mode
  as the proof; compare side-by-side with classic before the user picks the
  channel default.
