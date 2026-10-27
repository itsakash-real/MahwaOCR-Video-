# Genius Knight movie style — now the main long-form movie style

User order, 2026-10-27: replace main with the Genius Knight movie
editing style. This document is the pinned spec for that style. The
implementation is `manhwa_ocr/video/framepipe.py`; the CLI entry point
is `video movie`. This style replaced, on main, the idea that a
long-form movie build should go through the panelfit/pillar renderer:
that renderer is still present for its existing modes, but it is no
longer the movie default.

## What it is

The style of the delivered Genius Knight 6-chapter movie, consolidated
from the session's six chapter-specific Phase B renderers into one
generic renderer. No chapter script, panel, or narration text is copied
here — only the editing rules.

- **Frame:** 1920x1080, 30 fps, full-bleed 16:9 centre crop. Every
  output frame is a full 1920x1080 crop: no pillars, no blur backdrop,
  no floating, no scrolling/traversing, zero white space by
  construction.
- **Motion — Solo rotation, very slow, in shot/run order:**
  `zoom_in -> slide_up -> slide_down -> zoom_out -> zoom_in ...`
  Zooms span 1.00 <-> 1.05, centred. Slides travel exactly 115 output
  pixels over the shot (base 1920x1195, cropped to 1920x1080, so no
  slide ever exposes an edge). Frame 0 of every shot is its full
  settled composition: no fade, no slide-in, no settle ramp.
  Consecutive shots reusing the same source + window are one run: the
  motion runs continuously over the run (an invisible hold).
- **Cuts:** hard cuts only. No fades, no transitions, no 1.5s
  slide-in. No captions, no title cards, no endscreen, no intro
  anywhere, no outro, no chapter breaks in the combined movie.
- **Sync:** one shot per narration sentence, sentence-synced spans
  (never a fixed 2-seconds-per-image rhythm — that rhythm was
  intro-only). Frame counts are allocated by largest remainder so
  total frames == round(narration seconds x 30) exactly.
- **Panels:** approved cuts only. A cut image is exactly 900 px wide
  and is cropped vertically only (full width, `y0` to `y0 + win_h`).
  A missing file, a wrong width, a window outside the image, or a
  source outside the approved cuts dir is a hard fail. The renderer
  never silently substitutes a pipeline / re-detected panel.
- **Coverage:** every narration sentence is used exactly once, every
  mapped shot is used. Skip only blank / white-only / bubble-only
  panels, and report every skip.
- **Voice:** Deepgram aura-2-odysseus-en @ 1.3 for movie mode
  (`video.movie` in `manhwa_ocr/config.py`).
- **Correction:** the session's `genius-knight.yaml` still said
  `mode: panelfit` / speed 1.35. That file is stale and must not be
  copied alone. The delivered movie was framepipe at 1.3.
- **Combined movie:** chapter videos joined by stream-copy concat
  (`video movie --concat-inputs a.mp4,b.mp4 --out movie.mp4`):
  no intro, no breaks, no re-encode.

## What was preserved, explicitly

- The Shorts path (`video/shorts.py`, `video/shorts` action) is
  untouched: same code, same defaults.
- The Solo placed path (`video.mode == "placed"`, Panel Placement
  Rule) is untouched: same code, same defaults.
- Classic / wide / adaptive / panelfit / fullframe renderers are
  untouched. Only the movie entry point changed: `video movie`
  always uses framepipe, regardless of `video.mode`.

## How to use it (local clone)

```bash
git pull origin main
# One chapter, from a shots JSON + narration audio:
python -m manhwa_ocr video movie --shots shots_ch1.json --audio audio_ch1.wav \
  --out Genius_Ch1_1080p.mp4 --cuts-dir cuts/ch1_recap --total 387.08
# Combined movie, no breaks:
python -m manhwa_ocr video movie --concat-inputs Ch1.mp4,Ch2.mp4,Ch3.mp4,Ch4.mp4,Ch5.mp4,Ch6.mp4 \
  --out Genius_Movie_1080p.mp4
```

Shots JSON is a list of objects:
`{"src": "cuts/.../GK1-p28-05.png", "y0": 120, "win_h": 800,
"start": 0.0, "end": 3.2, "sentence": 1, "panel": "GK1-p28-05"}`.
`start`/`end` are seconds in the narration track; `frames` may be
given explicitly instead of `--total`.

## Gates (run before delivery, never forced past)

1. Approved-cuts gate (above) — hard fail.
2. Sentence coverage gate (above) — hard fail.
3. Render: 1920x1080, 30 fps, duration == narration duration.
4. Re-verification: first / mid / last frame of EVERY shot, key
   subject fully visible through the whole motion path. The CLI
   extracts those frames and asserts 1920x1080; near-white share
   above 30% is flagged for review, not silently passed. Fixes are
   window moves / re-pairs only; audio is untouched.
5. Deliver only when all chapters + the combined movie are verified.
   List residues openly (unfixable shot, new cut needed) for a user
   decision; do not smooth them over.
