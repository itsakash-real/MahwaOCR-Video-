# Transcription performance: map, root causes, and the fixes on `fix/transcription-speed`

Written October 2026, from a full read of the transcription path plus the live
session data (`the-unlimited-bacteriomancer`, `copying-skills-with-affinity`,
`chronicles-of-the-demon-slayer` under `Documents/ManhwaRecaps`).

Target: any chapter, URL to `chapter_N.md`, in ≤10 minutes wall-clock, with no
possible hang: every stage bounded, checkpointed, resumable.

---

## Phase 1 — the map (URL → chapter_N.md)

```
chapters.txt link
  └─ scrapers.discover_pages()          one HTML fetch, timeout=60s, no retry (bounded)
       └─ images.download_images()      8 threads, starts spaced ≥ download.delay (0.25s),
                                        ≤ download.max_retries attempts, 1.5·n backoff,
                                        60s timeout per request; URL-keyed page cache;
                                        pages already on disk are reused
            └─ images.build_tiles()     8 threads; per-page slicing into ~2200px tiles with
                                        15% overlap, gutter-snapped cuts; tile files cached
                                        behind a settings fingerprint; landscape (credits
                                        banner) pages skipped [NEW]
                 ├─ AGENT MODE (prepare/assemble/finalize)
                 │    prepare   writes manifest.json + TASK.md (or TASK_1..K.md shards,
                 │              auto-sized: ceil(tiles / pipeline.agent_shard_tiles=20)
                 │              [NEW; was: --shards, default 1 = unbounded])
                 │    [READ]    EXTERNAL agents read tile images and write
                 │              tile_results_shard_K.json, incrementally [NEW protocol];
                 │              not a CLI command — the only stage the tool cannot time
                 │              internally (measured from artifacts in the timing report)
                 │    assemble  deterministic merge/dedupe (flatten) + writes ASSEMBLE.md
                 │    [ANSWER]  EXTERNAL agent writes assembled.json
                 │    finalize  merges series memory, writes chapter_N.md, saves tile
                 │              notes, prints + persists the timing report, cleans scratch
                 └─ PROVIDER MODE (run)
                      stage 1   one vision() call per tile (or vision_batch), ThreadPool
                                at pipeline.concurrency; per-call timeout=180s, ≤4 attempts
                                with exponential backoff; content-hash cache per tile;
                                rolling no-progress watchdog (stall_minutes=10) [NEW]
                      stage 2   one text() call per chapter (bounded same way)
                      write     chapter_N.md + memory + tile notes + timing report [NEW]
```

Wall-clock, measured (before the fixes):

- Agent mode, `chronicles-of-the-demon-slayer` ch11 (74 tiles): prepare 21:53 →
  finalize 22:26 = **~33 minutes**; the tile reading by one serial agent is
  essentially all of it. This is the "30-50 minutes per chapter" baseline.
- Provider mode is configured `concurrency: 1, vision_batch: 1` (free-tier
  RPM), so a chapter is 74-105 *sequential* vision calls: 15-70+ minutes.
- Downloads/slicing/assembly/finalize are seconds each (timing logs confirm:
  prepare 4-16s, assemble ~1s, finalize ~1s).

Context economics (approx.): a tile is 690-1100px × ~2000-2400px ≈ 1.5-2.5K
image tokens for a current VLM. A 74-105-tile chapter is 150-260K tokens of
images alone — beyond any single reading context that also has to emit results;
a 20-tile shard is ~50K, safely inside every agent we drive.

## Phase 2 — ranked root causes (evidence)

Hangs:

1. **Agent-mode reading was unbounded by design** — `--shards` defaulted to 1
   (`cli.py`), `next` never mentioned shards, so one agent read a whole
   chapter of images in one context and died mid-chapter. Evidence:
   `copying-skills-with-affinity` ch1 prepared shards:1/105 tiles/0 results;
   `the-unlimited-bacteriomancer` ch1 stalled at 32/74 tiles at 04:30 with no
   progress after (the "stuck for 2 hours" run). **Fixed:** auto-sharding.
2. **No staleness watchdog** — `tile_progress()` had no time dimension; a dead
   reader left the chapter "in progress" forever and `next` kept pointing at
   the same TASK.md. **Fixed:** `next` flags a silent reading stage
   (`pipeline.stale_minutes`) and says to restart the missing workers.
3. **Shard progress was invisible** — `tile_progress()` counted only
   `tile_results.json`; a sharded chapter (which writes only
   `tile_results_shard_K.json`) showed "0 of N read" forever. **Fixed.**
4. **Provider-mode stage 1 had no wall-clock bound** — each call is bounded
   (180s × 4 attempts) but a trickling/hung endpoint made the stage crawl for
   hours looking busy. **Fixed:** rolling no-progress watchdog, loud failure.

Slowness:

1. **Serial tile reading** (agent mode: one context; provider mode:
   concurrency 1) — the dominant cost. **Fixed (agent mode):** TASK.md now
   instructs parallel shard workers. Provider mode: config, left to the user's
   RPM budget.
2. **Results written at the end, not incrementally** — a dying reader lost
   everything; restarts redone work. **Fixed:** the shard protocol requires
   rewriting the results file after every tile.
3. **Asura credits banner read as a tile** — copying-skills ch1 page 1 is a
   1532×1024 banner. **Fixed:** landscape pages skipped when tiling.

Not causes (verified): re-downloads (URL-keyed cache), re-slicing
(fingerprint cache), memory reload per tile (stateless), scraper HTML fetch
(bounded, no retry loop), download loop (bounded retries, rate-limited).

## Phase 3 — options considered

| Option | Accuracy vs bar | Speed | Cost | Integration | Verdict |
|---|---|---|---|---|---|
| **Parallel sharded vision-LLM reading (chosen)** | Same model, same prompt, ≤20 images per context — accuracy unchanged, arguably better (less context pressure) | ~shard-count× faster read phase; end-to-end ~6-10 min | free (agent mode) | already built (shards existed) | **do it** |
| Bigger `vision_batch` (provider mode) | measurably worse: multi-image VLM degradation (arXiv 2601.07812) | fewer round-trips | per-token | small | only as a user option; not the answer |
| Dedicated OCR (PaddleOCR / manga-ocr) | OCR-only: no scene descriptions, no speaker attribution — fails the quality bar the recap writer needs; manga-ocr is Japanese-specialized, base PaddleOCR-VL ~27% full-sentence on manga | very fast per bubble | free/local | large (new stage, mapping, review) | reject for transcription; keep as a *cross-check* (video already has ocrcheck) |
| Two-pass (fast OCR + LLM repair where confidence low) | Same bar problem: pass 1 must still describe scenes, so it doesn't get cheaper | moderate | free/local | large | reject for now |
| Paid OCR/vision APIs (Gemini/Anthropic) | strong | fast | paid | small | user's standing preference is no external APIs; not wired |

Sources: [multi-image VLM degradation](https://arxiv.org/html/2601.07812v1),
[manga-ocr](https://github.com/kha-white/manga-ocr),
[PaddleOCR vs Tesseract](https://gigagpu.com/paddleocr-vs-tesseract-vs-easyocr),
[PaddleOCR-VL manga fine-tune](https://github.com/jzhang533/paddleocr-vl-sft),
[MangaVQA/MangaLMM benchmark](https://arxiv.org/html/2505.20298v3).

## What changed (branch `fix/transcription-speed`)

1. `prepare` auto-shards every chapter: `ceil(tiles / pipeline.agent_shard_tiles)`
   (default 20); `--shards N` still overrides. TASK files instruct parallel
   workers, incremental writes, and resume-without-redo.
2. Shard-aware progress: `next`/`status`/dashboard count every results file;
   a fully-read sharded chapter advances to ASSEMBLE instead of looking stuck.
3. Reading staleness watchdog in `next` (`pipeline.stale_minutes`, default 30).
4. Provider-mode stage-1 no-progress watchdog (`pipeline.stall_minutes`,
   default 10): rolling deadline, loud UserError, cached tiles kept.
5. Landscape credits pages are skipped when tiling (tile ids keep original
   page numbers; the page file remains for video).
6. Per-chapter timing report: `.work/timing/chapter_N.json` + a printed table
   at completion, both modes.

`chapter_N.md` bytes are unchanged in format; recap and video behavior is
untouched.
