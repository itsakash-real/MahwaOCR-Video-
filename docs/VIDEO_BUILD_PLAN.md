# Video Build Plan — recap videos synced to narration

**Status:** built, phases 0–8. Covered by `tests/test_video.py`.

**Where the build differs from this plan:**
- **Editing style v2 (plan version 2).** This follows `manhwa_recap_style_spec.md`:
  - Hard cuts replace crossfades.
  - Shots have `layout` (single/pair/inset/wide), `background`
    (blur/black/white) and `cards` (panel + focal `crop` + CapCut-style
    `enter` + `motion` + `at`).
  - The pair layout's first card starts centred and slides left as the second
    slides in from the right.
  - Entrances take 0.6–0.8s. Camera moves ease out over at most 2.6s, then
    hold (static frames are reused, which keeps renders fast).
  - Even-timed plans target 2.7s shots and pick a spread of panels instead of
    squeezing all of them in.
  - Speech-bubble/SFX-only panels (`text_only`) are skipped.
  - Effects are calm by default: only shake, only on `{impact}`.

  The phase 3–5 details below describe v1 (crossfade, scroll, the per-shot
  `panel`/`motion`/`transition` fields) and are kept for history.
- **Frames are drawn in Python, not with FFmpeg filters.** Each frame is drawn
  with sub-pixel precision (Pillow + numpy) and piped to FFmpeg for encoding.
  FFmpeg's `crop` filter can't change size per frame, and `scale`/`zoompan`
  snap to whole pixels, so slow zooms jitter.
- **Shots are independent segments** (hard cuts). Joining them is a stream
  copy, and editing one shot re-renders only that shot. Each card's crop is
  pre-scaled once per shot. 1080p renders at about real time with 2 workers
  on a 4-core laptop.
- **Without whisper, tagged recaps still use their tags.** The plan places the
  tagged panels, with sentence times estimated from text length
  (`sync: "estimated"`).
- **Recap TTS text.** Recaps also write `recaps/chapter_N.tts.txt`, plain
  spoken text ready to paste into a TTS tool.

The rest of this document is the original plan, kept for reference.

**For agents (Antigravity, Claude Code):** work one phase at a time. A phase is
done only when its acceptance checks pass and `python -m pytest tests/ -q` is
green. Don't start the next phase in the same change. Follow the conventions
in `AGENTS.md`: `--session` on every command, `UserError` for anything the user
can fix, atomic writes, no scratch files outside `.work/`.

---

## 1. What we're building

For each chapter, the user records the recap script as audio and drops it in the
series folder. The tool turns that chapter's pages into a 1080p recap video:

- Each **panel** is shown on its own, centred on a blurred, enlarged copy of
  itself (the standard recap look).
- **Camera motion** per panel: slow zoom in or out, pan, or a top-to-bottom
  scroll for tall panels. Crossfades between panels.
- **Synced to the narration**: a panel appears on the sentence that describes
  it. Effects land on specific words: a zoom punch on *emphasised* words, a
  shake on impacts, a flash on reveals.
- **Optional music** that gets quieter whenever the narrator speaks.
- Output: `video/chapter_N.mp4`, ready to upload or polish in an editor.

The flow is the same as everything else in the project: a command produces a
file, the agent (or the user) checks or edits it, and the next command consumes
it. The video is fully determined by an editable **plan file**, so fixing a bad
panel choice means editing one line and re-rendering.

---

## 2. Decisions already made

These came from the tool research. Don't revisit them without a reason.

| Area | Decision | Why |
|---|---|---|
| Rendering | **FFmpeg filtergraphs, run from Python with subprocess** | Fastest on CPU, gives the same output for the same input, easy to script. |
| FFmpeg binary | `ffmpeg` on PATH if present, else **`imageio-ffmpeg`**'s bundled binary | Nothing to install by hand on Windows. |
| Panel detection | **Our own gutter scanner** (numpy) | Webtoon panels are separated by blank horizontal bands. Every ML panel detector found is AGPL (ultralytics/YOLO) or non-commercial (Magi). |
| Word timestamps | ~~faster-whisper~~ **pywhispercpp (whisper.cpp)** — switched later | MIT, Windows wheels, no PyTorch, no CUDA errors. faster-whisper's ctranslate2/MKL backend turned out to crash (`mkl_malloc: failed to allocate memory`) under ordinary desktop memory pressure; ggml doesn't have that failure mode. |
| Alignment | **Fuzzy-match** whisper's words against our own script | We know the exact text, so timing is all we need from whisper. |
| Video format | **16:9 1920×1080 at 30fps first**; 9:16 in Phase 8 | The plan file keeps format separate, so adding 9:16 later is cheap. |
| Encoding | libx264, `-crf 20 -preset veryfast -pix_fmt yuv420p`, AAC 192k | Plays everywhere, YouTube-friendly. |

**Avoid:** Remotion (not open source), Editly (headless-gl breaks on Windows),
Kumiko / aeneas / whisper-timestamped (AGPL), Magi (non-commercial), stable-ts
(archived), any YOLO-based detector. FFmpeg's `zoompan` filter jitters on slow
moves because it snaps to whole pixels: animate with `crop` expressions on an
input upscaled 2–3× instead.

---

## 3. Series folder layout after this work

```
<session>/
  chapters.txt                 existing
  series_memory.json           existing
  chapter_N.md                 existing transcript (gains hidden panel markers, Phase 3)
  recaps/
    chapter_N.md               script to record (clean text, no tags)
    chapter_N.cues.json        NEW  sentence -> panels, emphasis, sfx (Phase 3)
  audio/                       NEW  user drops chapter_N.mp3 / .wav / .m4a here
  media/                       NEW  kept page images + panel data
    chN/
      pages/page_001.png ...   kept after transcription (no longer deleted)
      panels.json              panel boxes + descriptions (no duplicate images)
      panels_sheet.png         contact sheet for a quick visual check
  video/                       NEW
    chapter_N.plan.json        editable shot list: the single source of truth
    chapter_N.timeline.json    word timings from the audio (Phase 4)
    chapter_N.storyboard.png   one frame per shot plus its sentence, for review
    chapter_N.mp4              the output
  .work/
    video/chN/segments/        per-shot render cache (scratch)
```

`media/`, `audio/` and `video/` hold large binaries. `start` should write a
`.gitignore` for them in each new series folder.

---

## 4. New CLI surface

All commands take `--session` and follow the existing CLI conventions.

```bash
python -m manhwa_ocr video panels     --chapter N     # split pages into panels
python -m manhwa_ocr video align      --chapter N     # added later: decide which panel per sentence, see below
python -m manhwa_ocr video plan       --chapter N     # write/refresh the shot list
python -m manhwa_ocr video sync       --chapter N     # word timings from the audio
python -m manhwa_ocr video render     --chapter N     # render the MP4
python -m manhwa_ocr video render     --chapter N --preview   # fast 480p proxy
python -m manhwa_ocr video storyboard --chapter N     # review image
python -m manhwa_ocr video build      --chapters 1-7  # all of the above, skips finished steps
python -m manhwa_ocr video status                     # per chapter: audio? plan? video?
```

`video align` (and `chapter_N.alignment.json`) didn't exist in the original
design below — `video plan` used to decide panel selection itself, reading
`cues.json`'s tags directly (see Phase 4). That turned out to let a wrong tag
(or a sentence with nothing to show, like an internal thought) through
silently. `video align` is now the only place a panel is chosen; `video plan`
only translates its decisions into shots. See `AGENTS.md`'s "Making videos"
section for the current pipeline and rules.

`next` gains video steps. Once a chapter has a transcript, a recap and an audio
file but no video, it prints the exact `video build` command.

Dependencies go in optional extras so OCR-only users aren't affected:

```toml
[project.optional-dependencies]
video = ["imageio-ffmpeg>=0.5", "numpy>=1.24"]
sync  = ["pywhispercpp>=1.5"]   # was faster-whisper; see the decisions table above
```

When an extra is missing, commands raise `UserError` with the exact install
line.

---

## 5. Phases

### Phase 0 — Foundations

**Goal:** stop deleting the images video needs, and make FFmpeg callable.

- `workspace.py` / `agentflow.py` / `pipeline.py`: on finalize, **move**
  `.work/chN/pages/` to `media/chN/pages/` instead of deleting it. Tiles are
  still deleted. For `--folder` runs, copy the pages in (renamed `page_NNN.png`,
  in natural order).
- New `manhwa_ocr/video/ffmpeg.py`:
  - `ffmpeg_exe()`: PATH first, then `imageio_ffmpeg.get_ffmpeg_exe()`, else
    `UserError("pip install -e \".[video]\"")`.
  - `run(args, timeout)`: subprocess wrapper. On failure, raise `UserError` with
    the last 15 lines of stderr.
  - `media_duration(path)`: parse `Duration:` from `ffmpeg -i` stderr. **The
    `imageio-ffmpeg` bundle has no `ffprobe`**, so don't rely on it.
- `audio_path(session, chapter)`: first match of `audio/chapter_N.{mp3,wav,m4a,ogg,flac}`.
  Apply `normalize_label`, so `chapter_01.mp3` matches chapter 1.
- `ensure_pages(session, chapter)`: if `media/chN/pages` is missing (chapters
  finished before this change), re-download from the chapter's link in
  `chapters.txt`, or tell the user to use `--folder`.

**Accept:** a finished chapter keeps `media/chN/pages`. `ffmpeg_exe()` resolves
on a clean Windows install with only the extra installed. A 3-second test MP3's
duration is read correctly.

### Phase 1 — Panel splitting

**Goal:** a chapter becomes an ordered list of panels with stable IDs.

- New `manhwa_ocr/video/panels.py`.
- **Panels cross page boundaries.** Webtoon pages are arbitrary cuts of one long
  strip, so a panel can start on page 3 and end on page 4. Process pages as one
  continuous strip: walk them in order and carry the unfinished bottom of each
  page into the next. Don't concatenate everything into one image: 30 pages ×
  16,000px is past `Image.MAX_IMAGE_PIXELS`.
- **Gutter detection:** reuse the row "flatness" score from
  `images._row_detail`, vectorised with numpy across the whole page. A gutter is
  ≥ 24px of consecutive flat rows. Don't assume white: compare against the
  row's own mean so black and coloured backgrounds work too.
- **Cleanup rules:** drop strips < 80px tall (stray SFX, credits). Merge a
  fragment < 200px into the nearer neighbour. Trim flat margins inside each
  panel. Flag panels taller than 2.5× the frame aspect as `tall` (these
  scroll). Skip the first and last pages if they're scanlation credit pages
  (detected by very low detail, or configurable).
- **IDs:** `P{page:03d}-{n:02d}`, using the page where the panel *starts*, e.g.
  `P003-02`. They stay stable across re-runs because they depend only on the
  page images and slicing settings.
- **`panels.json`:** for each panel: `id`, `index`, `segments` (one or more
  `{page, y0, y1}` when it spans pages), `x0, x1`, `width`, `height`, `tall`,
  `text_density` (from overlapping tile results if they still exist), and
  `scene` (the `scene` descriptions of overlapping tiles, joined). Boxes only:
  panels are cropped from `media/chN/pages` at render time, with no duplicate
  image files.
- `panels_sheet.png`: thumbnails in a grid, labelled with IDs. This is how a
  human or the agent checks the split.
- Settings in `manhwa-ocr.yaml` under `video.panels` (gutter height, min panel
  height, merge threshold).

**Accept:** synthetic strips with known panel boundaries (including one panel
split across two pages, black gutters, a tiny SFX fragment) split exactly as
expected. Your real chapter 1 gives a sensible contact sheet; check it by eye
and record the panel count in the PR.

### Phase 2 — Plan and render (even timing)

**Goal:** a complete, watchable video with no sync yet. The fallback path stays
forever.

- New `manhwa_ocr/video/plan.py`. Plan schema (versioned):

```json
{
  "version": 1,
  "chapter": "3",
  "format": {"width": 1920, "height": 1080, "fps": 30},
  "audio": "audio/chapter_3.mp3",
  "audio_duration": 287.4,
  "sync": "even",
  "music": null,
  "shots": [
    {"panel": "P003-02", "start": 0.0, "end": 6.2,
     "motion": "zoom_in", "transition": "fade",
     "effects": [], "sentence": null}
  ]
}
```

- **Even timing:** split the audio's length across panels, weighted by text
  density and area. Clamp each to 1.5–8s. If the audio is longer than the
  panels at max hold, cycle through key panels with alternating motion. Never
  freeze on one image for 30 seconds.
- **Motion presets** (deterministic, picked by panel shape, alternating so
  consecutive shots differ): `zoom_in`, `zoom_out`, `pan_down`, `pan_up`,
  `scroll` (tall panels, always top to bottom), `hold` (tiny drift). Strength
  around 6–10% zoom; slow beats fast.
- New `manhwa_ocr/video/render.py`, **one segment per shot**:
  1. Crop the panel from its page(s) with Pillow, stitching vertically when it
     spans pages, and write it to `.work/video/chN/src/`.
  2. FFmpeg: `split` → background `scale` to cover the frame + `crop` +
     `boxblur` + darken a little → foreground upscaled 2× then animated with
     `crop` expressions in `t` → `scale` to fit about 90% of the frame →
     `overlay` centred.
  3. Encode the segment to `.work/video/chN/segments/<hash>.mp4`. The hash
     covers the shot spec, panel pixels and render settings, so **after
     editing the plan, only changed shots re-render**.
  4. Join segments with `xfade` (0.4s), mux the audio, `-shortest` off: the
     video length equals the audio length.
- Render segments in parallel, up to `os.cpu_count() // 2` at a time.
- `--preview` renders 854×480 with `-preset ultrafast` for quick checks.
- Write the MP4 atomically: render to a temp name, then rename.

**Accept:** a 4-panel synthetic chapter with a 12s test tone renders an MP4 that
is 1920×1080 at 30fps, matches the audio duration within 0.1s, and has an
audio stream. Re-rendering with one shot changed re-encodes one segment. Your
real chapter renders in under about 6 minutes on your laptop; record the time.

### Phase 3 — Panel references through transcripts and recaps

**Goal:** the recap script knows which panel each sentence is about.

- **Agent mode `assemble`:** `merge.flatten` already labels blocks by tile.
  Add the tile id to each line, e.g. `(dialogue) Raon: ... {p003_t02}`. Then
  map tiles to panels by vertical overlap: a tile can cover two panels, so
  keep both.
- **Assembly prompt** (`prompts.py`, bump `PROMPT_VERSION`): after each scene
  header, add a hidden marker listing its panels,
  `<!-- panels: P003-01 P003-02 -->`. The visible transcript is unchanged.
- **Recap prompt** (`script.py`): add a **panel list**, giving each panel's ID
  and its `scene` description from `panels.json`. Ask for narration where each
  sentence starts with a tag: `{P003-02}`, or several `{P003-02,P004-01}`, or
  `{same}` to keep the current panel. Also allow `{beat}`, `{impact}` and
  `{reveal}` cues.
- `RecapBuilder._write_recap` splits this into:
  - `recaps/chapter_N.md`: clean text with all `{...}` tags removed. **This is
    what the user records.** It should read exactly as before.
  - `recaps/chapter_N.cues.json`: `[{sentence, panels, emphasis_words, cues}]`,
    in order.
- Validation: unknown panel IDs are dropped with a warning; sentences without
  tags inherit the previous panel. If under 50% of sentences got a valid tag,
  warn and offer `--force`.
- Existing recaps without a cues file keep working: the plan falls back to even
  timing. `video status` shows "no panel tags — `recap --force` to enable
  sync".

**Accept:** a recap of a synthetic chapter gives a clean `.md` (no braces) and a
cues file whose panel IDs all exist in `panels.json`. The old recap format
still compiles and renders.

### Phase 4 — Sync to the audio

**Goal:** panels switch on the sentence that describes them, within about 0.3s.

- New `manhwa_ocr/video/sync.py`:
  - `transcribe(audio)`: pywhispercpp (whisper.cpp) with word-level timestamps
    (`token_timestamps=True, split_on_word=True, max_len=1`), model from config
    (default `base.en`; `tiny.en` for speed/low RAM; multilingual models for
    Hindi narration). Audio is decoded to mono 16kHz PCM with our own bundled
    ffmpeg first, so whisper.cpp never needs ffmpeg on PATH. Cache the result in
    `video/chapter_N.timeline.json`, keyed by the audio file's hash, so it runs
    once per recording.
  - `align(script_sentences, words)`: normalise both sides (lowercase, strip
    punctuation, spell digits out consistently), run `difflib.SequenceMatcher`
    over the word sequences, and take each sentence's first matched word as its
    start. Sentences with no match get times interpolated from their
    neighbours.
  - Report: % of sentences matched, plus the worst 5 to check by ear.
- The plan uses the aligned times when cues exist: one shot per sentence. With
  several panels in one sentence, split its time between them. With `{same}`,
  extend the shot. Very short sentences (< 1.2s) merge into the previous shot
  unless they change panel.
- Lead time: a panel appears 0.15s **before** its sentence starts, so the cut
  doesn't feel late. This is configurable.
- If pywhispercpp isn't installed, `video plan` uses even timing and says how
  to enable sync.

**Accept:** a synthetic test using a hand-made word list with known timestamps
(no real audio or model in unit tests) aligns sentences to within one word.
Ad-libbed, skipped and mispronounced words still align. On your real chapter:
listen through and report the match %; aim for ≥ 90% of sentences matched.

### Phase 5 — Recap-style effects

**Goal:** the punch that makes it feel edited, not a slideshow. Every effect has
a plan field and a config switch.

| Effect | Trigger | Implementation |
|---|---|---|
| Zoom punch | `*emphasised*` word in the script, at its word timestamp | Fast 6% zoom in over 0.12s, ease back over 0.5s (`crop` expression keyed on `t`) |
| Shake | `{impact}` cue, or `[SFX: ...]` in the transcript for that panel | 0.3s ±12px jitter, deterministic (seeded by shot hash) |
| Flash | `{reveal}` cue | 0.15s white fade in and out (`fade` on a white `color` layer) |
| Beat hold | `{beat}` / `[Beat]` cue | Hold the panel through the pause with a slow push-in |
| Music bed | `music:` in config or `--music file` | `sidechaincompress` keyed on the narration: music ducks to about −18 dB under speech, about 0.4s release; music loops if shorter |
| Outro | End of the chapter | 1s fade to black, audio fade 0.8s |

Rules: at most one punch per 3s, at most one shake per shot, and no effect in
the first 0.5s of a shot.

**Accept:** each effect renders in isolation on a synthetic shot. A test checks
that the effects in the plan match the cues. Visually check one real chapter.

### Phase 6 — Agent review loop

**Goal:** the Antigravity agent can catch and fix bad panel choices on its own.

- `video storyboard --chapter N` writes `video/chapter_N.storyboard.png`: one
  frame per shot in a grid, each labelled with its time, panel ID and the
  sentence being spoken. The agent can **look at this image** and judge whether
  the panel matches the words.
- `video render --preview` produces a fast proxy.
- A review task file: `.work/video/chN/REVIEW.md`, written by `video plan`. It
  holds a checklist plus the exact edit format, for example "to change shot
  12's panel, edit `shots[11].panel` in `video/chapter_N.plan.json`", and then
  the re-render command.
- `next` gains a step: plan exists, storyboard not reviewed → "review the
  storyboard". The agent marks the review done by writing `"reviewed": true` in
  the plan. That flag is optional; skipping the review just renders.
- `AGENTS.md` gets a "Making videos" section: the commands, the plan schema,
  and the rule to **edit the plan, never the MP4 or the segments**.

**Accept:** an end-to-end test: build a plan, change one shot's panel the way
an agent would, render. Exactly one segment re-renders and the plan validates.

### Phase 7 — Dashboard

**Goal:** see and play everything without opening folders.

- A **Video** tab per series. Per chapter it shows: audio present (with
  duration), recap has panel tags, sync match %, plan, storyboard and video
  status, plus *Copy agent prompt: build videos*.
- **In-browser playback:** serve `video/*.mp4`. `http.server` doesn't support
  HTTP Range, and browsers need it to seek in video. Implement single-range
  `Range: bytes=a-b` handling with 206 responses. Serve only files listed by
  the series (same id-lookup pattern as documents, never raw paths).
- Storyboard and panel contact sheet viewable in the drawer.
- Optional: **upload audio from the dashboard.** Accept POST to
  `/api/session/<slug>/audio/<chapter>`, audio content types only, size-capped
  (200 MB), written atomically to `audio/chapter_N.<ext>`. Same write-header
  guard as the existing endpoints.

**Accept:** the dashboard test suite gains Range tests (206, correct
`Content-Range`, out-of-range rejected) and traversal tests for media. Manually
play a rendered chapter in the browser and seek in it.

### Phase 8 — Shorts (9:16) and clips

- `--format shorts` gives 1080×1920 with the same plan logic. Panels fill more
  of the frame; the blur background is mostly hidden on tall panels.
- **YouTube Shorts are capped at 3 minutes**, so a full chapter won't fit.
  `video clip --chapter N --from 45 --to 105 --format shorts` cuts a highlight
  from the same plan.
- `video compile --chapters 1-7` joins chapter MP4s with title cards between
  chapters, to match the recap `compile` command.

---

## 6. Testing approach

- **No network and no real model in unit tests.** Generate synthetic strips
  with Pillow (known panel boundaries) and synthetic audio with FFmpeg's
  `sine`/`anullsrc` sources. Use hand-made word-timestamp lists for alignment.
  Mark tests that need FFmpeg `@pytest.mark.video` and skip them cleanly when
  it's absent.
- Check renders by probing, not by watching: resolution, fps, duration
  (±0.1s), stream count.
- Golden tests for plan generation: same inputs → identical plan JSON.
- Each phase adds tests in `tests/test_video_<phase>.py`, and the full suite
  must stay green.

## 7. Risks and how they're handled

| Risk | Mitigation |
|---|---|
| Panels detected wrong on art-heavy pages with no clean gutters | Contact sheet for review. Panel split settings in config. Manual override: `panels.json` is editable, the same way the plan is. |
| Whisper mishears character names | Alignment matches whole word sequences, so a few wrong words don't break it. Pass character names from series memory as whisper's `initial_prompt`. |
| Render time on a slow laptop | Segment cache, parallel segments, `--preview`, `veryfast` preset. |
| Disk use from kept pages (30–100 MB per chapter) | `clean --media --chapter N` after the video is final. The `.gitignore` keeps it out of git. |
| A user-recorded script drifts from the text | Interpolation covers gaps. The match % report flags chapters to check by ear. |
| Copyright claims on uploads | Not a tool issue. The user decides what to publish. |

## 8. Rough effort

| Phase | Size |
|---|---|
| 0 Foundations | small |
| 1 Panels | medium (tuning on real chapters) |
| 2 Plan + render | large (the core) |
| 3 Panel tags | medium (prompt work, then re-running recaps) |
| 4 Sync | medium |
| 5 Effects | medium |
| 6 Agent review | small |
| 7 Dashboard | medium |
| 8 Shorts | small–medium |

**Usable video after Phase 2. Synced video after Phase 4.** Phases 5–8 can be
reordered to suit what you want first.

## 9. Open questions for the user

1. 16:9 only, or 9:16 Shorts too? The plan assumes 16:9 first and Shorts in
   Phase 8.
2. Which TTS or voice will you use? If it can export word timestamps itself,
   Phase 4 can read those directly and skip whisper.
3. Background music: do you have tracks to use? If so, where should they live
   (one per series, or a shared folder)?
