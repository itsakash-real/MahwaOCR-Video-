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
Then, for that chapter:

1. Read the tile images (batch a few at a time) and write `chN/tile_results.json`.
2. `python -m manhwa_ocr assemble --chapter N` → writes `ASSEMBLE.md`.
3. Answer that prompt into `chN/assembled.json`.
4. `python -m manhwa_ocr finalize --chapter N` → writes the chapter, updates memory.

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
(`pairs.json`, `plan.json`, `tile_results.json`, `parts.json`, ...).

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

Only one recap task exists at a time. If `--prepare` says a task is already in
progress, **finish it with `--finalize`** — don't discard work. Use
`recap --cancel` only if the user asks. If you're interrupted partway, finalize
saves the chapters you've written and keeps the task open for the rest.

Each prompt's series context is limited to what was known *by that chapter*,
so don't add knowledge from later chapters into an early chapter's narration.

The task's prompt files already contain each chapter's transcript and series
context. Work through them in order; each one is told the previous chapter's
opening hook so segments flow into each other.

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

Each chapter becomes a 1080p recap video cut to the user's recorded narration,
in the style of `manhwa_recap_style_spec.md`:

- **Hard cuts**, about one shot per sentence (about 2.7s on average, 1–5s).
- **Cards, not full panels.** Each shot shows a focal crop of a panel (faces,
  action) as a card with a white border, rounded corners and a black drop shadow.
- **Layouts:** `single` (one card centred), `pair` (the first card starts
  centred, then slides left while a second slides in from the right), `inset`
  (a card over a blurred close-up of another panel) and `wide` (full width).
- **Backgrounds:** `blur` (a blurred, darkened copy of the panel), `black` or
  `white`.
- **Calm motion.** CapCut-style entrances (`fade_in`, `slide_up`, `blur_in`,
  `zoom_in`, `fit_in_frame`, `swing_right`, `pull_down`, …) take 0.6–0.8s.
  The camera move eases out over at most 2.6s, then holds still.
- **Effects** are tied to cues and off by default, except for a small shake on
  `{impact}`.

The design is in `docs/VIDEO_BUILD_PLAN.md`.

Only build a video for a chapter once the user has put its narration in place:
`audio/chapter_N.mp3` (or `.wav`, `.m4a`) in the series folder, or uploaded from
the dashboard. `next` tells you which chapters are ready. Then:

```bash
python -m manhwa_ocr video build --chapters 1-7 --session "<name>"
```

That splits panels, then — for every chapter that has panel tags — **explicitly
runs the alignment step** described below as its own pipeline stage before
planning and rendering. Steps that are already current are skipped.

**The production pipeline is `video align → video plan → video render`.**
For a chapter with panel tags, `chapter_N.alignment.json` is the **single
source of truth for which panel shows for every sentence**. Nothing downstream
re-decides this:

- **`video align --chapter N`** reads the recap's tags, the narration audio and
  the panels, and deterministically decides, per sentence: is this even
  something a panel can show? `INTERNAL`/`PURE_AUDIO` sentences (a thought, a
  sound with no visible actor) hold whatever panel is already on screen
  (`"display_action": "HOLD_PREVIOUS"`, `"selected_panel": null`) — no panel
  search is attempted for them at all. Everything else gets
  `"display_action": "SHOW"` with a concrete `"selected_panel"`. It writes
  `video/chapter_N.alignment.json` and runs its own validator — 8 structural
  checks (timestamps monotonic, every panel id exists, no backward jumps, no
  unexplained jumps, panels never shown outside their own interval, no
  gaps/overlaps, non-visual segments never change panel, segment durations are
  sane). **This validation must pass before planning or rendering will
  proceed.** It also prints a review queue: sentences its own evidence (the
  recap's tag, sequence order, lexical overlap with each panel's own OCR'd
  text) couldn't resolve with real confidence — this is now the *only* place a
  panel-content judgment call happens, and it's a short, bounded list instead
  of the whole chapter.
- **`video plan --chapter N --force`** *only translates*
  `chapter_N.alignment.json` into the renderer's shot format — crops, layouts,
  entrances, pairing two shots side by side for presentation. **It makes no
  panel-selection decisions of its own: not from `cues.json`'s tags, not from
  OCR, not from CLIP/embeddings, not from the narration text, not from any
  heuristic or LLM judgment.** `cues.json` is read for exactly one thing here —
  whether the chapter has tags at all, which decides whether the no-tags
  "even" spread applies or an alignment is required. Once a chapter has tags,
  `video plan` **requires** a `chapter_N.alignment.json` that exists, parses,
  and whose own `"validation"."status"` is `"PASS"`. If it's **missing,
  malformed, or failed validation, planning fails explicitly** and tells you to
  run `video align --chapter N` — it never falls back to reinterpreting the
  recap's tags itself.
- **The renderer follows the alignment's decision exactly and never infers a
  replacement:** `"display_action": "SHOW"` with a `"selected_panel"` shows
  exactly that panel; `"display_action": "HOLD_PREVIOUS"` with
  `"selected_panel": null` keeps whatever panel is already on screen.

If `video align`'s review queue isn't empty, use the checkpointed review
template in `PROMPTS.md` (in the output folder) to resolve it before treating
the chapter as done — small batches, a real preview clip per batch, the user
confirms each one. **"Validation passed" is not "nothing to check."**
Validation only catches structural problems (missing panels, bad timing,
backward jumps); it can't tell whether a panel's *content* matches the
sentence — that's exactly what the review queue and `PROMPTS.md`'s checkpoint
loop are for.

**Pairing check before aligning.** The alignment engine places already-correct
tags into shots deterministically — it is not a substitute for tagging the
right panel in the first place. Before running `video align`, run:

```bash
python -m manhwa_ocr video pair --chapter N --session "<name>"
```

Open every `pairing_NN.png` listed in `.work/video/chN/PAIRING.md`. Each row is
one sentence: green = its tagged panel(s), grey = the next panels in the story.
For each row: does the green panel show what the sentence says? If a grey one
fits better, use it. Edit `.work/video/chN/pairs.json` (sentence number -> list
of panel ids, in story order; two ids = shown one after the other). Rules:
never a TEXT/credits panel, never the same panel for two sentences far apart,
keep story order. Then run the full pipeline, in order:

```bash
python -m manhwa_ocr video pair --chapter N --apply --session "<name>"
python -m manhwa_ocr video align --chapter N --session "<name>"
python -m manhwa_ocr video plan --chapter N --force --session "<name>"
python -m manhwa_ocr video render --chapter N --session "<name>"
```

The video itself enforces: each panel shown once (no repeats), panels never cut
at the sides (only top/bottom), no text-only panels, captions/bubbles cropped
out where they sit at a panel's top or bottom.

**Review loop:**
1. Open the storyboard images listed in `.work/video/chN/REVIEW.md`. Each shot
   shows its cards, its time, its layout, background and entrances, and the
   sentence spoken over it.
2. If a card's *panel* doesn't match its sentence, the fix belongs in the
   pairing tags, not the plan: edit `.work/video/chN/pairs.json` (see "Pairing
   check" above, or `PROMPTS.md` for an alignment issue the pairing review
   missed), then re-run `video align` and `video plan --force` for that
   chapter. You may still hand-edit `video/chapter_N.plan.json` directly for
   pure presentation — `crop`, `enter`, `motion`, `layout`, `background`,
   `effects` — but **never `cards[k].panel`**: that field is the alignment's
   decision, copied in by `video plan`; changing it in the plan doesn't change
   the alignment, so the next `video align`/`video plan --force` silently
   overwrites your edit.
3. Set `"reviewed": true` and run `video render --chapter N --session "<name>"`.
   Only edited shots re-render.

Rules:
- **Fix a wrong picture in pairs.json/the recap tags, never in the plan.** The
  plan only carries presentation fields now; which panel shows for which
  sentence lives in `chapter_N.alignment.json`, produced by `video align`.
- A plan with hand edits or `reviewed: true` is never overwritten automatically.
  `--force` rebuilds it and discards edits; don't use it unless asked.
- Sync needs panel tags in the recap. If `video status` shows "Untagged", the
  recap predates tagging. **Ask the user before fixing it**, because the fix is
  `recap --chapters N --prepare --force` (rewrite with tags), and then they
  must re-record. Without tags the video still builds, just with even timing —
  and no alignment step applies, since there is nothing to align.
- **"estimated" vs "synced" in `video status`'s plan column is not cosmetic.**
  "synced" means `video align`'s whisper step actually time-aligned the
  narration audio; "estimated" means whisper failed (often low RAM —
  `mkl_malloc`-style errors) and shot timing was guessed from sentence length,
  so the picture visibly drifts from the voice as the chapter goes on.
  `video render` refuses an "estimated" plan by default for exactly this
  reason. If you hit that refusal: don't pass `--force` to push through it —
  tell the user whisper is failing, suggest lowering `whisper_model` in
  `manhwa-ocr.yaml` (e.g. `small.en` → `base.en` or `tiny.en` if the machine is
  RAM-constrained), then re-run `video align --chapter N` and
  `video plan --chapter N --force`, and check `video status` again for
  "synced" before rendering.
- **`video render` also refuses by default when `video align`'s review queue
  for that chapter isn't empty** — it means at least one sentence's panel was
  never confirmed by a human, and rendering anyway risks shipping the next
  vampire-panel-style mistake unseen. The error names the flagged sentence
  numbers. Don't pass `--force` to push through it — open the pairing sheets
  for those sentences (`.work/video/chN/pairing_NN.png`) or use `PROMPTS.md`'s
  checkpointed review template, fix `pairs.json` if anything's wrong, then
  `video align --chapter N` and `video plan --chapter N --force` again before
  rendering.
- Plans made for the old editing style are rebuilt automatically by
  `video build`. If such a plan had hand edits, it stops and asks for
  `video plan --chapter N --force`; ask the user before running that.
- `--preview` renders a fast 480p check; `video clip --from A --to B --format 9:16`
  makes a Short; `video compile --chapters 1-7` joins chapters with title cards.

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
video/        panels, cues, whisper sync, plan, frames, render, clips
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

225 tests, including a regression test for every bug found in the full audit
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
