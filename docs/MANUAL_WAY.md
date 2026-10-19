# Manual way — do it by hand, no agent

Do every step yourself with shell commands. Nothing here edits the program.
Use the venv Python for any whisper/sync step (system Python lacks
pywhispercpp, so align silently falls back to estimated timing):

```bash
VENV=~/workspace/venvs/manhwa-ocr/bin/python   # adjust to your machine
```

The tool is driven only with `python -m manhwa_ocr <command>`. Never use
`--workdir` (it bypasses session isolation). Always pass
`--session "Your Series Name"`.

## 0. Setup

```bash
python -m manhwa_ocr session list
python -m manhwa_ocr start "Your Series Name" <chapter-url-1> <chapter-url-2>
# or, for an existing series, put chapter URLs (one per line) in
# <session>/chapters.txt, then:
python -m manhwa_ocr status --session "Your Series Name"
```

## 1. Transcript (one chapter at a time, in order)

```bash
python -m manhwa_ocr prepare --chapter N --session "Your Series Name"
# read <session>/chN/TASK.md, read the tile images, write chN/tile_results.json
python -m manhwa_ocr assemble --chapter N --session "Your Series Name"
# answer the ASSEMBLE.md prompt into chN/assembled.json
python -m manhwa_ocr finalize --chapter N --session "Your Series Name"
```

Repeat for each chapter, in order (memory is order-dependent).

## 2. Recap

```bash
python -m manhwa_ocr recap --session "Your Series Name" --chapters N --prepare
# read the TASK.md it prints, write .work/recap/parts.json
python -m manhwa_ocr recap --session "Your Series Name" --finalize
```

Recap rules (enforced by `--finalize`):
- No intro/outro. Exact plot/order/details, never invent events, numbers, dialogue.
- Names exactly as in SERIES CONTEXT. Third person only.
- Tag EVERY sentence with its panel (`{P003-02}`, `{same}`, `{impact}`).
  Target 95%+ tagged. Never tag a text-only or credits panel.
- Spell numbers as words in the spoken text. No runs of very short sentences.
- Hook/open loops (P3): open with a hook — the first 30 seconds must give
  proof (the exact rank/number/beat the title promises), stakes, and a
  one-line roadmap. Mark it `[HOOK]` on its own line before the sentence.
  Open a loop every 2–3 minutes: `[LOOP: the question]` on its own line.
  End on an open question/cliffhanger, not a summary.
  `[HOOK]` / `[LOOP: ...]` are editing cues, never spoken.

## 3. Record narration (you, not the tool)

Record `recaps/chapter_N.tts.txt` and save it as `audio/chapter_N.mp3`
(or `.wav` / `.m4a`). Never rewrite a recap whose chapter already has
audio unless you record it again. The tool never creates audio.

## 4. Video — pair / align / plan / verify / review / approve / render

```bash
python -m manhwa_ocr video pair --chapter N --session "Your Series Name"
# open every pairing sheet; fix pairs.json only where a sentence's panel is wrong
python -m manhwa_ocr video pair --chapter N --apply --session "Your Series Name"
$VENV -m manhwa_ocr video align --chapter N --session "Your Series Name"
python -m manhwa_ocr video plan --chapter N --session "Your Series Name"
python -m manhwa_ocr video verify --chapter N --session "Your Series Name"
python -m manhwa_ocr video review --chapter N --session "Your Series Name"
python -m manhwa_ocr video approve --chapter N --session "Your Series Name"
python -m manhwa_ocr video render --chapter N --session "Your Series Name"
# -> video/chapter_N.mp4 (1080p)
```

`video build --chapter N` does align + plan + render in one step, but run
the steps separately the first few times so you see each gate.

### P0–P5 checkpoints (check these every chapter)

```bash
python -m manhwa_ocr video status --session "Your Series Name"
```

- **Timing warnings:** any `timing_warnings` entry means estimated timing.
  Read its `fix` (e.g. install pywhispercpp) and fix it — never render on
  estimated timing. Render refuses estimated timing by design.
- **Mask-risk:** a panel flagged `mask_risk` (shouting face + dominant
  bubble) is DROPPED and re-paired, never masked with a grey oval.
  Bubble painting was removed entirely: no bubble is ever painted, the
  crop steers around bubbles, and a panel with no bubble-free crop is
  dropped and re-paired.
- **Layout switches:** a `layout_switches` entry means the planner
  auto-switched a crop to the pair-safe shape (e.g. 4:3 → 3:4). No action
  needed; the switch is logged in the plan + QA.
- **Hook:** `hook` shows explicit-hook yes/no + open-loop count. If the
  hook is missing, add a `[HOOK]` sentence to the recap and rebuild.
- **Chapter cards / callouts / SFX / music:** the `editor` dict in
  `video status` shows counts plus which SFX/music files are present or
  missing. Missing files render silent on that layer, never fail.
- **Captions:** new plans carry `captions` (3–5 words at a time, key word
  yellow), timed from the alignment sentences and rendered in a
  dedicated black bottom bar below the cards (cards capped to the top
  92% of the frame, so captions never cover panel text). Old plans
  have none — run `video captions --chapter N` to add them, then
  render again.
- **Beat sync / grade:** only if you set `video.beats` (or drop
  `music/beats.json`) do cuts snap to beats (±0.15s max); `video.grade:
  "subtle"` is the only grade. Both default off.

### Storyboard fixes (the only hand-edit routes)

| You want to change | Edit this | Then |
|---|---|---|
| Which picture a sentence shows | recap tags (`recaps/chapter_N.cues.json`) or `pairs.json` | `video pair --apply`, `video align`, `video plan --force` |
| Drop a panel, head position, exact crop, a shot's background, how a card enters, chapter-card title, callout text | `video/chapter_N.fixes.json` (`drop`, `face`, `crop`, `shot`, `chapter_cards`, `callouts`) | `video plan --force` |
| Anything else about presentation | nothing by hand | a setting in `manhwa-ocr.yaml` |

Never hand-edit `video/chapter_N.plan.json` — `plan --force` rebuilds it
and re-applies `fixes.json`. `fixes.json` is validated as a whole: one
bad entry refuses the file.

## 4b. Intro hook + previously (30s each, before a part)

```bash
# Hook: write intro/intro.txt (one sentence per line, [HOOK] / [LOOP: ...]
# markers), record it as audio/chapter_0.mp3 (30s), then:
python -m manhwa_ocr video hook --chapters 1-6 --audio audio/chapter_0.mp3 \
    --script intro/intro.txt --session "Your Series Name"
# -> video/chapter_0.mp4, picks reviewed in intro/intro.auto.json
# Manual instead: video intro --prepare, fill intro/intro.json, video intro.

# Previously (between parts): write previously/previously_7.txt, record it
# as audio/previously_7.mp3 (30s), then:
python -m manhwa_ocr video previously --chapters 1-6 --audio audio/previously_7.mp3 \
    --script previously/previously_7.txt --session "Your Series Name"
# -> video/chapter_prev7.mp4. Panels ONLY from chapters 1-6, never the next part.
```

Full rules: `INTRO_HOOK_GUIDE.md` + `PREVIOUSLY_GUIDE.md`. Picks are
auto (lexical overlap + face/impact heuristics, no repeats, no
text-only/credits, payoff guard). Used panels/beats are recorded in
`video/intro_memory.json` and avoided next time unless a sentence is
marked `[REUSE]`. The 30-second rule: target 30s, 20–45s configurable
(`video.hook` in config or `--duration`); audio is never trimmed.

## 5. Shorts + end-screen

```bash
python -m manhwa_ocr video shorts --chapter N --session "Your Series Name"
# -> video/chapter_N.shorts_1..3.mp4 + video/chapter_N.shorts.json
python -m manhwa_ocr video endscreen --chapter N --session "Your Series Name"
# -> video/chapter_N.endscreen.json + video/chapter_N.watch_next.txt
```

See `SHORTS_GUIDE.md` for the upload workflow. Shorts slice your recorded
narration — they never re-record or generate audio.

## 6. Multi-chapter video (optional)

```bash
python -m manhwa_ocr video compile --chapters 1-7 --session "Your Series Name"
# -> video/compiled_1-7.mp4, title cards between chapters
python -m manhwa_ocr compile --chapters 1-7 --session "Your Series Name"
# -> recaps/compiled_1-7.md, one long script with a runtime table
```

## 7. Publish (you)

Upload `video/chapter_N.mp4`, add chapters/timestamps from the plan's
chapter cards, paste the WATCH NEXT block from `watch_next.txt`, then
upload the 3 Shorts (titles + hooks in `shorts.json`). See
`SHORTS_GUIDE.md`.
