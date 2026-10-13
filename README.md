# manhwa-ocr

Transcribe webtoon/manhwa chapters with a vision model, keeping **character and plot context across chapters**.

Works with Claude, Gemini, any OpenAI-compatible endpoint (OpenRouter, Groq, LM Studio, vLLM), or local models via Ollama. No vendor SDKs — plain HTTP.

**New here? Read [USER_GUIDE.md](USER_GUIDE.md).** It walks the whole job step
by step, with copy-paste prompts for Antigravity:

1. Paste chapter links.
2. The agent transcribes them.
3. The agent writes recap scripts.
4. You record the narration.
5. The agent builds the videos.

Agents read [AGENTS.md](AGENTS.md).

---

## Why not just use Tesseract?

Traditional OCR fails on comics. The fonts are stylized, text sits on top of artwork, and speech bubbles have no consistent layout. Tesseract and EasyOCR produce garbage on manhwa pages.

This tool uses a **vision model** that reads the page the way a person does — it sees the art, understands who's talking, and distinguishes dialogue from narration from sound effects.

## The two things that make it work

**1. Tall-strip slicing.** Webtoon pages are delivered as single images around `900 × 16000` pixels. Hand one to a vision model whole and it gets downscaled to roughly **110 pixels wide** before the model ever sees it. Text becomes unreadable and the model starts inventing lines from context.

This tool slices each page into overlapping full-resolution tiles, and snaps each cut to a flat "gutter" row between panels so speech bubbles don't get bisected. This is the single largest quality difference versus naive approaches.

**2. Persistent series memory.** After each chapter the model writes a structured update to `series_memory.json` — characters (with **appearance descriptions**), world rules, terminology, open plot threads. That file is injected into the prompt for every subsequent chapter.

The effect: a character introduced in chapter 1 as "silver hair, red eyes, black gloves" gets correctly identified by name when they reappear unnamed in chapter 6. Without this, every chapter starts blind.

---

## Install

```bash
cd manhwaOCR
pip install -e .              # transcripts + recaps
pip install -e ".[video,sync]"   # + recap videos (FFmpeg, pywhispercpp)
```

Dependencies: `requests`, `pillow`, `pyyaml`, `numpy`. Video adds `imageio-ffmpeg`
and `pywhispercpp` (whisper.cpp, not faster-whisper — avoids its ctranslate2/MKL
memory-allocator crashes on ordinary desktops).

If the `manhwa-ocr` command isn't on your PATH after install (common on Windows), use the module form everywhere instead — it behaves identically:

```bash
python -m manhwa_ocr run <url> --chapter 1
```

## Sessions

Each series lives in its own isolated session, so character bibles never bleed
across series. Switching is instant and nothing is lost.

```bash
manhwa-ocr session new "The Hero Cannot Rest"   # creates and activates
manhwa-ocr session new "Some Other Series"

manhwa-ocr session list      # * marks the active one
manhwa-ocr session use "The Hero Cannot Rest"   # switch back; all data intact
manhwa-ocr session info      # chapters + memory state
```

Every other command uses the active session automatically. Per-command override:
`--session "Some Other Series"`.

Series folders live in the folder set by `output_folder:` in `manhwa-ocr.yaml`.
That is `../ManhwaRecaps` by default: a folder next to the program folder, so
code and output stay apart. If there is no config, they go in
`~/.manhwa-ocr/sessions/`. `$MANHWA_SESSIONS_ROOT` or `--sessions-root`
override it for one shell or one command.

`session archive` never deletes. It moves the directory under `_archived/` and
prints the path.

## Chapter links

Paste your chapter URLs into the session's `chapters.txt`, one per line in
reading order. Chapter numbers come from line order, so the URLs don't need to
follow any pattern.

```bash
manhwa-ocr links path        # prints the file to paste into
manhwa-ocr links add "https://..." "https://..."
manhwa-ocr links list        # per-chapter done/pending
manhwa-ocr status            # summary + what's next
```

Override numbering with `47 = https://...` on a line.

## Quick start

```bash
# 1. Set your key
export ANTHROPIC_API_KEY=sk-ant-...        # bash
$env:ANTHROPIC_API_KEY = "sk-ant-..."      # PowerShell

# 2. Create a session and add links
manhwa-ocr session new "My Series"
manhwa-ocr links add "https://site.com/ch/1" "https://site.com/ch/2"

# 3. Run everything pending
manhwa-ocr run --all-pending
```

Output lands in the session directory:

```
<session>/
├── chapter_1.md          ← the transcript
├── chapter_2.md
├── chapters.txt          ← your link list
├── series_memory.json    ← the story bible, grows each chapter
├── .cache/               ← per-image results; re-runs are nearly free
└── ch1/
    ├── pages/            ← downloaded originals
    ├── tiles/            ← sliced tiles
    └── raw_blocks.txt    ← stage-1 extraction, for debugging
```

## Agent mode — no API key needed

If your models come bundled with an agentic IDE (Antigravity, Cursor, Claude
Code) rather than as raw API access, the driving agent can do the vision work
itself:

```bash
manhwa-ocr prepare --next          # downloads + slices, writes TASK.md
#   agent reads tiles -> tile_results.json
manhwa-ocr assemble --chapter 1    # writes ASSEMBLE.md
#   agent answers -> assembled.json
manhwa-ocr finalize --chapter 1    # writes chapter + updates memory
```

Each step is a plain file handoff. See `AGENTS.md`, which agents read
automatically, and `USER_GUIDE.md` for the full walkthrough.

## Local images

If a site is JavaScript-rendered or blocks scripts, don't fight it. Save the images with your browser and point the tool at the folder:

```bash
manhwa-ocr prepare --folder ./my-saved-pages --chapter 3
```

## Other backends

```bash
# Gemini (handy if you're driving this from Antigravity)
manhwa-ocr run --next \
  --provider gemini --model gemini-2.0-flash \
  --api-key-env GEMINI_API_KEY

# Local, free, offline
manhwa-ocr run --next --provider ollama --model llama3.2-vision

# OpenRouter / Groq / Together / anything OpenAI-shaped
manhwa-ocr run --next \
  --provider openai \
  --base-url https://openrouter.ai/api/v1 \
  --model qwen/qwen-2.5-vl-72b-instruct \
  --api-key-env OPENROUTER_API_KEY

# LM Studio on localhost
manhwa-ocr run --next \
  --provider openai --base-url http://localhost:1234/v1 --model local-model
```

Local vision models are noticeably weaker at stylized comic lettering than hosted ones. Expect more `[unclear: ...]` markers. The slicing helps a lot here — small models benefit most from seeing full-resolution tiles.

## Dashboard

```bash
manhwa-ocr ui        # http://127.0.0.1:8765
```

A local, single-page dashboard for everything the pipeline has produced: every
series, per-chapter OCR and recap status, compiled scripts, character memory, and
a reader with copy/download. Tick chapters to generate ready-made agent prompts
for exactly those chapters. It binds to localhost only, uses no third-party
libraries, and doesn't run the pipeline itself.

## Recaps

```bash
manhwa-ocr recap   --session "My Series" --chapters 1-4,11 --prepare   # agent mode
manhwa-ocr recap   --session "My Series" --finalize
manhwa-ocr compile --session "My Series" --chapters 1-4,11
```

One narration file per chapter under `recaps/`, then compile any selection into a
single video script with a runtime table. Existing recaps are skipped unless
`--force`. Without `--prepare`, `recap` calls the configured provider directly.

## Recap videos

```bash
pip install -e ".[video,sync]"
manhwa-ocr video build --chapters 1-7     # needs audio/chapter_N.mp3 per chapter
```

Turns each chapter into a 1080p recap video synced to your recorded narration.

1. **Panels.** Pages are split into panels at their gutters, with panels that
   cross page boundaries handled.
2. **Panel tags.** The recap writer tags each sentence with the panel it
   describes.
3. **Align** (`video align`, deterministic, no AI judgment at render time).
   Whisper (pywhispercpp) finds when each sentence is spoken; for each
   sentence, a rule-based classifier decides whether it's even something a
   panel can show (a thought or a sound with no visible actor holds whatever's
   already on screen, no search attempted), and otherwise picks a panel from
   the story's own forward sequence, trusting the tag unless the panel's own
   OCR'd text gives stronger evidence against it. Writes
   `chapter_N.alignment.json`, the single source of truth for panel selection,
   and validates its own output (panels exist, no backward/unexplained jumps,
   no timing gaps, …) before anything downstream may use it.
4. **Frames.** Recap-channel editing:
   - hard cuts about every 2.7s;
   - focal crops shown as bordered cards with drop shadows;
   - single, side-by-side (slide-in) and inset layouts;
   - blurred, black or white backgrounds;
   - calm CapCut-style entrances (fade, slide, blur, zoom, fit in frame,
     swing, pull down);
   - eased camera moves that settle and hold, all drawn with sub-pixel
     precision.
5. **Plan.** `video plan` only translates the alignment's decisions into an
   editable `plan.json` (crop, layout, entrance) — it never re-decides which
   panel to show. Reviewed through a storyboard image.

Editing one shot re-renders only that shot. Also: 9:16 Shorts clips,
multi-chapter compilations, ducked background music. Design and trade-offs are
in `docs/VIDEO_BUILD_PLAN.md`.

## Inspect the memory

```bash
manhwa-ocr memory show     # rendered briefing, exactly as the model sees it
manhwa-ocr memory path     # where the JSON lives
manhwa-ocr memory reset --yes
```

`series_memory.json` is plain JSON and **meant to be hand-edited**. If the model gets a character's name wrong early, fix it there and every later chapter inherits the correction. Commit it to git alongside your transcripts.

---

## How it works

```
chapter URL
    │
    ├─ scrape page image URLs           scrapers.py
    ├─ download, normalize to PNG       images.py
    ├─ slice tall strips into tiles     images.py  ← quality-critical
    │
    ├─ STAGE 1 (vision, parallel)       pipeline.py
    │    one call per tile → {scene, blocks[]}
    │    series memory injected into every prompt
    │    cached by image hash + prompt version
    │
    ├─ dedupe overlapping tile text     pipeline.py
    │
    ├─ STAGE 2 (text, one call)         pipeline.py
    │    merge tiles → markdown transcript
    │    propose memory updates
    │
    └─ merge into series_memory.json    memory.py
```

Stage 1 is expensive and cached per image. Stage 2 is cheap. That split means you can iterate on the assembly prompt without paying for the vision pass again.

## Reproducibility

Runs are deterministic as far as the models allow:

- `temperature: 0.0` by default
- the model id is pinned in config
- `PROMPT_VERSION` in `prompts.py` is part of the cache key — bump it when you edit a prompt, or stale cached results will be reused

Same inputs plus same `series_memory.json` gives you the same output. Note that memory is **order-dependent**: running chapters 1→7 produces different context than 7→1. Process in order.

## Testing

```bash
pip install -e ".[dev]"
python -m pytest tests/ -q
```

225 tests covering config, slicing, providers, memory merge semantics, sessions, link numbering and resume, the agent flow, recaps and compile, scrapers, video (panels, sync, plans, rendering, clips) and the dashboard (including path-traversal, range and upload guards). `tests/test_audit.py` holds a regression test for each bug found in the full audit.

## Extending

**New site:** subclass `Scraper` in `scrapers.py`, implement `matches()` and `page_urls()`, add it to the `SCRAPERS` list. The generic scraper already handles most static reader pages.

**New backend:** subclass `Provider` in `providers.py`, implement `vision()` and `text()`, register it in `PROVIDERS`.

**Different output format:** edit `CHAPTER_PROMPT` in `prompts.py` and bump `PROMPT_VERSION`. Want a recap-video script instead of a transcript? That's a prompt change, not a code change.

## Tuning

| Symptom | Fix |
|---|---|
| Garbled or invented text | Lower `slicing.target_height` to 1000–1200 |
| Text cut mid-sentence between tiles | Raise `slicing.overlap` to 250 |
| Same line appears twice | Lower `pipeline.dedupe_threshold` to ~0.80 |
| Distinct lines wrongly merged | Raise `dedupe_threshold` to ~0.92 |
| Rate limit errors | Lower `pipeline.concurrency` to 1–2 |
| Cuts landing through artwork | Raise `slicing.gutter_search` to 300 |

## Cost

Roughly, for a 30-page chapter at ~90 tiles with a small hosted vision model: a few cents. Caching means re-runs after prompt edits cost almost nothing on stage 1. Local models via Ollama are free.

---

## Note on use

This tool transcribes images you point it at. Comics are copyrighted, and most reader sites prohibit automated access in their terms of service. Use it on material you have the right to process — your own scans, licensed copies, public-domain work, or content you have permission to use. Check the site's terms and `robots.txt` before scraping, keep `download.delay` set, and don't republish transcripts of licensed work.

MIT licensed.
