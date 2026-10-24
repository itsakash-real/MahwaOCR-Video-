# AGENTS.md

Instructions for AI coding agents (Antigravity, OpenCode, Cursor, Claude Code, Aider)
working in this repo.

**If you are driving the tool for the user (not editing its code):** use only
`python -m manhwa_ocr <command>`. Never run `python -c` snippets that `import
manhwa_ocr...`, never call `inspect.getsource(...)` on its modules, never load
its internal functions directly to "check" or "debug" something — not even to
read a file faster. If a command's output is confusing or an error doesn't say
what to do, **stop and show the user the exact command and error**; don't probe
the source to figure it out yourself. The "Verifying changes" section near the
bottom of this file, and anything under "Architecture"/"Extending", is for
whoever edits this package's code — it is not a hint to call those internals
from your own one-off scripts.

## Prompts

The rules every pasted prompt starts with (COMMON_RULES) and the prompts themselves live in one file,
`manhwa_ocr/agent_prompts.py`. USER_GUIDE.md ("Which prompt do I use?") lists them; the dashboard buttons, `next`,
REVIEW.md and the reviewer all use that file.
Do not copy prompt text into other files. Change `agent_prompts.py` and USER_GUIDE.md together; a test fails if they differ.

## What this project is

`manhwa-ocr` turns webtoon/manhwa chapters into recap videos:

- transcripts, made with a vision model;
- recap narration scripts;
- videos synced to the user's recorded narration.

Character and plot context is kept across chapters in `series_memory.json`. It
is a plain Python CLI. You drive it with shell commands, and nothing needs to
stay running.

## If you only read one section

When you start, resume, or are unsure what to do:

```bash
python -m manhwa_ocr next --session "<series name>"
```

It inspects the series on disk and prints the exact next action. Run it, do what
it says, run it again. **Keep looping until it tells you to stop.** Do not stop
after a single chapter unless the user asked for one.

Every command `next` prints already carries `--session`. **Copy them exactly.**
The user may switch the "active" series in the dashboard while you work; a
pinned `--session` keeps you on yours.

If a command fails, it prints `Error: ...` with what to fix (often a JSON file
you wrote, with the line number). Fix that and re-run the same command. Never
delete files to get past an error. `MANHWA_DEBUG=1` shows a full traceback.

## The workflow, and who does what

| Step | Who | `next` prints | Details |
|---|---|---|---|
| 1 | user | "has no chapter links yet" | The user pastes links into `chapters.txt`. Don't invent links. |
| 2 | **you** | `prepare` / `assemble` / `finalize` commands | [Agent mode](#agent-mode--when-you-have-no-api-key) |
| 3 | **you** | `STEP 2 (agent) - write recaps` | [Writing recaps](#writing-recaps) |
| 4 | user | `STEP 3 (you, not the agent) - record narration` | The user records `recaps/chapter_N.tts.txt` and saves it as `audio/chapter_N.mp3` |
| 5 | **you** | `STEP 4 (agent) - ... build the video` | [Making videos](#making-videos) |
| 6 | — | `STEP 4 done` | Everything is finished; it lists the compile/clip commands |

When `next` prints **"AGENT: nothing left for you to do"**, stop. Tell the user
which chapters need narration audio and where to save it (`next` prints the
exact path). You cannot record audio, and you must never create, convert or
generate audio files yourself. Run `next` again only after the user says the
audio is in place.

If steps 3 and 5 both appear (some chapters need recaps, others already have
audio), do both before stopping.

## The dashboard

`python -m manhwa_ocr ui` serves a local dashboard at http://127.0.0.1:8765. The
user watches progress there while you work in the terminal. You never need to
interact with it — but everything you write to disk shows up there live, so
keep transcripts and recaps in their proper places.

## Sessions — one workspace per series

Every series gets its own isolated folder: transcripts, memory, recaps. Nothing
is shared, so passing the right `--session` is what keeps one series' character
bible out of another's.

```bash
python -m manhwa_ocr session list                  # * marks the active one
python -m manhwa_ocr start "Series Name" <urls...> # new series, creates AND activates
python -m manhwa_ocr session info                  # chapters + memory state (active series only)
```

`session info`/`session use`/`session list` act on the **active** series and do
**not** take `--session` — that flag only exists on the per-task commands
(`video`, `recap`, `next`, `status`, `links`, ...). Don't guess at flags; if a
command errors with something like "unrecognized arguments" or "No active
series", that's telling you which form it wants — re-read this section rather
than trying variations. The reliable pattern that needs no active-series state
at all: always pass `--session "<name>"` directly to the task command you're
running (`video status --session "<name>"`, `video pair --chapter 2 --session
"<name>"`, ...); you never need to `session use` first.

Without `--session`, task commands use the active series, and refuse to run if
there is none. They never fall back to writing in the current folder.

Never use `--workdir` to work around a session issue; it bypasses isolation.
Never archive a session unless the user explicitly asks.

## Resuming after an interruption

Everything is resumable, and `next` always knows where things stand:

- **Half-read chapter:** keep what is already in `tile_results.json` and add the
  remaining tiles. A chapter only counts as read when every tile id from
  `TASK.md` has an entry; `next` tells you how many are left.
- **Re-running `prepare`** is safe. If the tiles are unchanged, your results are
  kept. If they changed, old results are renamed `*.stale`.
- **`finalize` twice** is harmless.
- **Download failed:** `next` says so and asks for the images to be saved to a
  folder; then `prepare --chapter N --folder <dir>`.

## The chapter link list

The user pastes chapter URLs into `chapters.txt` inside the active session. You
do not need to ask them for chapter numbers — numbering comes from line order.

```bash
python -m manhwa_ocr status        # done / pending / what is next
python -m manhwa_ocr links list    # per-chapter detail
```

Use `--next` instead of `--chapter N` so you always pick up the first unprocessed
chapter. A chapter counts as done once `chapter_N.md` exists, so progress survives
restarts and you can resume a half-finished run safely.

**Loop until `status` reports nothing pending.** Do not stop after one chapter
unless the user asked for one.

## Agent mode — when you have no API key

If no provider key is available, **do not give up and do not ask the user to buy
API access.** Use agent mode, where you do the model work yourself:

```bash
python -m manhwa_ocr prepare --next        # or --chapter N, or --folder ./images
```

That downloads, slices, and writes `<session>/chN/TASK.md`. Read that file. It
contains the tile list, the exact prompt to apply, and where to write results.
Chapters are auto-sharded (`TASK_1.md`...`TASK_K.md`, sized from
`pipeline.agent_shard_tiles`): run the shard tasks **in parallel** (sub-agents,
up to the cap the index names) — each writes its own
`tile_results_shard_K.json` and rewrites it after every tile it reads, so an
interrupted worker loses at most one tile and finished shards are never redone.
Then, for that chapter:

1. Read the tile images (batch a few at a time) and write the shard results
   files (or `chN/tile_results.json` on a single-shard chapter).
2. `python -m manhwa_ocr assemble --chapter N` → writes `ASSEMBLE.md`.
3. Answer that prompt into `chN/assembled.json`.
4. `python -m manhwa_ocr finalize --chapter N` → writes the chapter, updates
   memory, and prints the per-chapter timing report
   (`.work/timing/chapter_N.json`).

Then run `status` again and repeat for the next pending chapter. Process **in
order** so series memory accumulates correctly.

## Scratch files

Everything a run generates lives under `<session>/.work/`. The session root holds
only deliverables: `chapter_N.md`, `recaps/`, `chapters.txt`, `series_memory.json`.

`finalize` deletes that chapter's scratch automatically — downloaded pages and
tiles are hundreds of MB per chapter. Pass `--keep` while debugging, or set
`pipeline.cleanup: false`.

Never write working files into the session root. If you need scratch of your own,
put it under `.work/`.

**Never edit the program itself** (anything under `manhwa_ocr/`, `tests/`, or
this repo's root). Don't write scripts into the program folder either. If the
tool does something wrong, stop and tell the user what you saw; don't patch the
code to get past it. Fix outputs only through the files these instructions name
(`pairs.json`, `fixes.json`, `cues.json`, `tile_results.json`, `parts.json`, `.work\agent_log.md`, ...).

## Writing recaps

Recaps are **one file per chapter**, in `<session>/recaps/chapter_N.md`. The user
can ask for any chapter or combination, by session name:

```bash
python -m manhwa_ocr recap --session "<name>" --chapters 3 --prepare
python -m manhwa_ocr recap --session "<name>" --chapters 1-4,11 --prepare --minutes 7
#   read the TASK.md it prints, write .work/recap/parts.json
python -m manhwa_ocr recap --session "<name>" --finalize
```

Chapters that already have a recap are skipped automatically, so asking for
`1-10` after doing `1-4` only writes 5-10. `--force` rewrites existing ones.

`prepare` also persists each chapter's writing brief (`recaps/chapter_N.brief.md`,
the exact prompts and context the writer was given; it survives finalize).
Finalize saves, next to each recap, a conformance + quality report
(`chapter_N.report.md`) and a grounding audit (`chapter_N.claims.json`). Read
both: fix WARN quality signals and FLAG claims before audio is recorded. For
recaps that already exist, `recap --report --chapters N` regenerates the two
reports without touching the recap. Finalize may also merge an optional
`memory_update` from parts.json into `series_memory.json` (same shape the
transcript stage uses) — supply one when the recap establishes something new.

Only one recap task exists at a time. If `--prepare` says a task is already in
progress, **finish it with `--finalize`** — don't discard work. Use
`recap --cancel` only if the user asks. If you're interrupted partway, finalize
saves the chapters you've written and keeps the task open for the rest.

Each prompt's series context is limited to what was known *by that chapter*,
so don't add knowledge from later chapters into an early chapter's narration.

The task's prompt files already contain each chapter's transcript, series
context, and a "story so far" built from every earlier chapter's recap, plus
the end of the immediately previous recap verbatim. Work through chapters in
order. **Every `parts.json` entry must include a `"summary"`** (3-5 plain
sentences: what happened, where each main character is now, what is still
unresolved) alongside `"narration"` and `"hook"` — this is what a NEW session
reads to pick the story back up, since it has no memory of earlier chapters
you wrote in a previous session. A missing or thin summary is exactly how a
new session loses the thread at the next chapter.

To turn recaps into one video script:

```bash
python -m manhwa_ocr compile --session "<name>" --chapters 1-4,11
# -> recaps/compiled_1-4_11.md, with a runtime table
```

`compile` only stitches existing recaps; it never writes narration. If chapters
are missing it says which, and the exact `recap` command to fill them.

Optional bookends: include `"_intro"` (~80 words) and `"_outro"` (~60 words) keys
in `parts.json` and `compile` will place them around the chapters.

**Tag every sentence with its panel.** The TASK.md lists each chapter's panels
and points at contact sheets (`media/chN/panels_sheet_*.png`). Look at them and
tag sentences as it describes (`{P003-02}`, `{same}`, `{impact}`, ...). The
video uses these tags to show the right picture. They are stripped from the
script the user reads and from `recaps/chapter_N.tts.txt`, the text they record.

**Never rewrite a recap whose chapter already has audio** (`audio/chapter_N.*`)
unless the user asks. Their recording would no longer match the script.

**The narration must be your own writing** — commentary on what happens, not a
re-reading of the chapter's dialogue. Quote the source only briefly, for lines
that genuinely land. This is both the house style and the correct way to handle
someone else's script. Do not paste transcript dialogue in bulk.

## Making videos

Taking a chapter all the way from its link to a finished video with
Deepgram doing the narration (no human recording involved): read
`docs/FULL_PIPELINE_WORKFLOW.md` in full before starting, and read it again
before starting each NEW chapter in the same session. It covers the
transcript/recap/consistency-check/voice/TTS-readiness/audio-verification
stages, then hands off to the file below for the video itself. The
`/make-recap-video` skill does this automatically.

Building or continuing a `video.mode: wide`/`adaptive` chapter video (any
multi-chapter continuation, e.g. chapter 1 then 2 then 3 in one session):
read `docs/CHAPTER_BUILD_WORKFLOW.md` in full before starting, and read it
again before starting each NEW chapter in the same session -- never rely on
memory of it from earlier in the conversation, even if you built an earlier
chapter a few messages ago. The `/build-chapter` skill does this
automatically; if asked directly instead, go read that file now.

Once the user has recorded `audio/chapter_N.mp3` (or `.wav`, `.m4a`), make the video
with these commands. Use the routine in this order, one chapter at a time:

```bash
python -m manhwa_ocr video pair    --chapter N --session "<name>"   # 1. check every picture against its sentence
python -m manhwa_ocr video pair    --chapter N --apply --session "<name>"   # 2. save your fixes from pairs.json
python -m manhwa_ocr video build   --chapter N --session "<name>"   # 3. align + plan (+ render, unless require_review is on)
python -m manhwa_ocr video verify  --chapter N --session "<name>"   # 4. non-AI check of every drawn panel
python -m manhwa_ocr video review  --chapter N --session "<name>"   # 5. vision reviewer: review, fix, rebuild, repeat
python -m manhwa_ocr video approve --chapter N --session "<name>"   # 6. approve this exact plan
python -m manhwa_ocr video render  --chapter N --session "<name>"   # 7. 1080p MP4 (+ rendered-frame QC)
python -m manhwa_ocr video qc      --chapter N --session "<name>"   # 8. re-run the frame QC on demand
```

`video build --chapters 1-7` does steps 3 and 7 for many chapters at once, with no agent review.
`video pipeline --chapter N` runs the whole ladder (panels → pair → align → plan →
verify → sync-audit → render → qc) resumable, with per-stage timing in
`.work/video/chN/pipeline.json`.

### What the tool does, in order (all deterministic)

1. **Align** (`video align`): decides which panel shows for each sentence, from the recap's tags
   and the narration's timing. Writes `video/chapter_N.alignment.json`. A sentence that carries
   several tags followed by an untagged one is reported by `video pair` (and by align's
   `no_unexplained_jumps` message) with the exact tag to move to which sentence; the suggestion is
   in `pairs.json` under `_suggestions` and is never applied for you.
2. **Coverage**: there is NO "every drawn panel must appear" pass.
   Time is scarce, panels are abundant — SELECT, don't cover. One
   tagged panel per sentence (≤8s: tagged holds the whole window);
   a sentence >8s may split (tagged ~65% + ONE impactful filler
   ~35%, only if the filler passes the impact test: named character
   + action beat + visible face, in story order, not a repeat/spoiler);
   a sentence >15s max 3 shots. Min shot 3.0s, ever; max shots =
   floor(audio_seconds / 3). A panel no sentence picked is simply not
   shown. `panels_shown / total` is INFO ONLY, never success.
3. **Plan** (`video plan`): turns the alignment into shots. Every card has one of the allowed
   shapes (landscape: 3:4, 1:1, 4:3, 16:9; portrait: 9:16, 3:4, 1:1), full width with no white
   gutters; a panel too wide for any shape is shown whole on a blurred backdrop. Every shot holds
   at least `min_hold` seconds (`manhwa-ocr.yaml`); a short sentence shares its shot with the next
   one. Crops avoid speech bubbles and never cut a detected head.
4. **QA** (`video/chapter_N.qa.json`): objective checks. The build stops only on hard failures: a
   card that isn't an allowed shape, a bubble in a card, or a shot too short to be seen at all
   (under 0.2s). A shot under the 3s floor is counted (`short_shots` in the QA file) and
   FAILS the sync gate — never fixed by stealing time from neighbours. Unshown panels are
   NOT a failure. QA reports tagged coverage per sentence, filler shots/seconds, min shot,
   and panels shown/total (info only).
5. **Render** (`video render`): 1080p MP4. After the encode, the
   **rendered-frame QC** pixel-checks the finished MP4 per shot — white
   margins inside the foreground, wall contact, zero motion for placed
   shots — and writes `video/chapter_N.frameqc.json` (full report, never
   truncated). A strict placed-mode violation stops the delivery; bands
   that carry art or are the art's own white (SFX overflow, foam, a logo's
   canvas) are reviewer notes, never failures. One renderer per output
   file: a second render targeting the same MP4 refuses (lock file).

### Where each kind of fix goes (this decides whether it survives a rebuild)

| You want to change | Edit this | Then |
|---|---|---|
| Which picture a sentence shows | the recap's tags (`recaps/chapter_N.cues.json`) or `pairs.json` | `video pair --apply`, `video align`, `video plan --force` |
| Framing: drop a panel, head position, exact crop, a shot's background, how a card enters, a chapter-card title, a callout's text | `video/chapter_N.fixes.json` (`drop`, `face`, `crop`, `shot`, `chapter_cards`, `callouts`) | `video plan --force` |
| Anything else about presentation | nothing by hand | change a setting in `manhwa-ocr.yaml` |

`video/chapter_N.plan.json` is **presentation only**. `video plan --force` rebuilds it from the
alignment and re-applies `fixes.json` every time, so a hand edit to the plan is lost. Never edit
`cards[k].panel` or any crop in the plan. `fixes.json` is validated as a whole: one bad entry
refuses the file and nothing is applied.

### Gates

- Timing that is only estimated (whisper unavailable) stops the render. Fix it by lowering
  `whisper_model` in `manhwa-ocr.yaml`. Do not use `--force` on render to skip it.
- Sentences flagged for review in the alignment are warnings, listed in the QA file.
  `video render --strict` makes them stop the render instead.
- The sync gate: `video render` (and so `video build`) audits the plan against the
  recording — wrong-picture time, the worst five-second windows, late cuts
  (`video.sync.gate_*`). A failing plan stops the render with the numbers; run
  `video sync-audit --chapter N` for the report, fix the alignment/plan, render
  again. Never pass `--force` to get past it.
- `video review` approves only when the reviewer approves. `video approve` approves the current
  plan by hand. With `video.require_review: true`, `video render` refuses unless the current plan is
  approved, and `--force` never bypasses that gate. Don't set `reviewed` in a plan or empty the
  review queue by hand; use `video approve`.
- Run `video build --chapter N --force` only to rebuild from scratch; ask the user first.

### Before the user records audio

The recap must be fixed before narration, because changing it later breaks sync. When you finish a
chapter's recap, check it against this list (`recap --finalize` refuses a tag on a text-only or credits
panel and warns about the rest):

- At least 90% of sentences carry a panel tag (aim for 95%).
- One drawn panel per sentence where you can; never tag a text-only or credits panel.
- Numbers are spelled out in `chapter_N.tts.txt` (the tool does this for new recaps).
- No run of three or more sentences under six words.
- About 7.5 or more words per drawn panel, so each panel gets its minimum time.
- **Never rewrite a recap whose chapter already has audio** (`audio/chapter_N.*`) unless the user
  asks. Their recording would no longer match the script.

### The intro (chapter 0)

A short trailer from panels of any chapters. The user records `audio/chapter_0.mp3`; you do:

```bash
python -m manhwa_ocr video intro --prepare --session "<name>"   # intro/intro.json template + contact sheets
python -m manhwa_ocr video intro --session "<name>"             # align, plan, render video/chapter_0.mp4
```

Fill `intro/intro.json` with one entry per narrated sentence: its text and 1-3 panels written like
`ch2:P010-14` (ids are labelled on `intro/sheets/*.png`). A panel may appear once, and text-only
panels are rejected. Framing fixes for the intro go in `video/chapter_0.fixes.json`, using the same
`ch2:P010-14` ids. `video intro --dry-run` plans without rendering.

**Tag every sentence with its panel.** Recap tags tell the video which panel a sentence is about.
Recap writing is the one AI step left in the video pipeline; everything after the tags is deterministic.

## Running it (provider mode)

Always prefer the module form — it works regardless of PATH:

```bash
python -m manhwa_ocr run --next           # next pending chapter
python -m manhwa_ocr run --all-pending    # everything left
python -m manhwa_ocr run "<url>" --chapter 1
python -m manhwa_ocr run --folder ./local-images --chapter 3
```

Config lives in `manhwa-ocr.yaml`. Flags override it.

## Two independent layers — do not confuse them

1. **You (the agent)** drive the CLI via shell. Any agent works.
2. **The vision backend** in `providers.py` does the actual reading. Set with
   `--provider`. These are unrelated: you can be running in Antigravity while
   the backend is Ollama — or use agent mode and be the backend yourself.

Available backends: `anthropic`, `gemini`, `openai` (any OpenAI-compatible
endpoint), `ollama`.

## Required environment

One API key, matching the chosen provider. Not needed in agent mode.

| Provider | Env var |
|---|---|
| `anthropic` | `ANTHROPIC_API_KEY` |
| `gemini` | `GEMINI_API_KEY` |
| `openai` | `OPENAI_API_KEY` (or set `--api-key-env`) |
| `ollama` | none; needs Ollama running locally |

If the key is missing the tool fails immediately with a clear message. Prefer
agent mode over asking the user to obtain a key.

## Before you change anything

- **`PROMPT_VERSION` in `prompts.py` is part of the cache key.** If you edit any
  prompt, bump it. Otherwise stale cached results get reused and output silently
  stops matching the new prompt.
- **`slicing.target_height` is quality-critical.** Webtoon pages are ~900x16000.
  Sent whole to a vision model they arrive ~110px wide and text becomes a guess.
  Do not disable slicing to "speed things up" — it is the main thing making
  output accurate.
- **Memory is order-dependent.** Chapters must be processed in order; 1→7 builds
  different context than 7→1.
- **`series_memory.json` is user-editable state, not generated output.** Never
  delete or regenerate it without asking. Corrections the user made by hand live
  there.

## Architecture

```
scrapers.py   find page image URLs for a chapter
images.py     download, normalize, slice tall strips into tiles
providers.py  vision/text backends (HTTP only, no vendor SDKs)
prompts.py    prompt templates + PROMPT_VERSION
memory.py     the series bible: merge, persist, render as briefing
sessions.py   per-series workspaces + the chapter link list
pipeline.py   provider mode: stage 1 = vision per tile, stage 2 = assembly
agentflow.py  agent mode: prepare / assemble / finalize file handoffs
script.py     recap writing (panel-tagged narration) and compile
status.py     `next` / `status`: what to do now, from what is on disk
video/        panels, cues, whisper sync, pairing, plan, fixes, verify/review, frames, render, clips, intro
ui/           the local dashboard
cli.py        argparse entry point
```

Stage 1 is cached per `image hash + provider + prompt version`. Stage 2 is cheap.
That split means prompt iteration on assembly does not re-pay for vision.

## Extending

- **New site:** subclass `Scraper` in `scrapers.py`, implement `matches()` and
  `page_urls()`, append to `SCRAPERS`. The generic scraper already handles most
  static reader pages.
- **New backend:** subclass `Provider` in `providers.py`, implement `vision()`
  and `text()`, register in `PROVIDERS`.
- **Different output format** (e.g. a recap script instead of a transcript): edit
  `CHAPTER_PROMPT` in `prompts.py` and bump `PROMPT_VERSION`. This is a prompt
  change, not a code change.

## Verifying changes

```bash
python -m pytest tests/ -q
```

356 tests, including a regression test for every bug found in the full audit
(`tests/test_audit.py`). Run them after any change to this package. They cover slicing geometry,
memory merge semantics, session isolation, the agent flow, link parsing, recaps,
the `next` workflow, and video (panels, sync, plans, frames, rendering, clips).

Quick manual check of the quality-critical path:

```bash
python -c "
from pathlib import Path
from manhwa_ocr.config import load_config
from manhwa_ocr.images import slice_tall_image
cfg = load_config('manhwa-ocr.yaml')
tiles = slice_tall_image(Path('ch2/page_02.png'), 2, Path('/tmp/t'), config=cfg.slicing)
print(len(tiles), 'tiles')
"
```

A correct slicer run on a 900x16000 page yields ~13 tiles, each the full 900px
wide, with no coverage gaps.

## Scope note

This tool processes images the user points it at. Comics are copyrighted and most
reader sites prohibit automated access. If asked to scrape at scale, or to
republish transcripts of licensed work, raise it with the user rather than
silently proceeding. Keep `download.delay` non-zero.
