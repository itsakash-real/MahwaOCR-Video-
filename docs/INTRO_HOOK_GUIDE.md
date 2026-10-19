# Intro / hook guide — the 30-second hook video (chapter 0)

The hook is the 30-second video that opens a part (or the series): proof,
stakes, roadmap, open loop. Two ways to build it; both end at the same
planner/renderer as a chapter video, and both output
`video/chapter_0.mp4`.

**You supply all audio.** The tool never creates, trims, or generates
audio. Record the hook narration first, then build.

## Auto hook (recommended)

```bash
# 1. Write the narration: one sentence per line. Markers are editing cues,
#    never spoken. See below for the structure they must fill.
cat > intro/intro.txt <<'EOF'
[HOOK]
He maxed every stat at the level 1 cap of 50.
Then the Death Lord's voice spoke from the trial gate.
[LOOP: will he survive the trial?]
This is the recap of chapters 1 to 6.
EOF

# 2. Record it as audio/chapter_0.mp3 (30 seconds: see "The 30-second rule").

# 3. Build: auto-pick panels, then plan + render.
python -m manhwa_ocr video hook --chapters 1-6 --audio audio/chapter_0.mp3 \
    --script intro/intro.txt --session "Your Series Name"
# -> video/chapter_0.mp4
```

Or from a cues file instead of a `.txt` script:

```bash
python -m manhwa_ocr video hook --chapters 1-6 --audio audio/chapter_0.mp3 \
    --cues intro/intro.cues.json --session "Your Series Name"
```

`video intro --auto` is the same auto build (same flags, same output).
`video hook --dry-run` plans without rendering.

### What auto-pick does

One panel per narration sentence, picked from across the given chapters:

- **Lexical overlap first:** the sentence's words against each panel's own
  OCR text (the same rule the chapter aligner uses).
- **Face / impact preference** on top, as a documented pixel heuristic,
  not vision: tall portrait panels read as close-ups, wide panels as
  establishing shots, low-dialogue panels as clean impact art.
- **Never** a text-only / credits panel. **Never** the same panel twice
  in one video.
- **Payoff guard:** picks walk story order and never run backwards, and
  an early sentence may only reach the early part of the panel pool
  (a proportional tease ceiling) — unless it names a later beat (one
  distinctive shared word with that panel's OCR text), which lifts the
  ceiling for that panel. A payoff panel can never appear before the
  narration reaches it.
- **Deterministic:** same script + panels + seed → same picks. Seed is
  `video.hook.seed` in config (default 7).

### Review the picks before you trust them

Auto-pick writes `intro/intro.auto.json`: every sentence with its picked
panel and the reason it was picked (`OCR overlap 12; portrait close-up
proxy; ...`). Read it. If a pick is wrong, edit the `panels` in that file
— then rebuild by pointing the hook at the edited file? No: the auto
file is a review record, not an input. To override a pick, switch to
the manual path below and put the corrected panel in `intro.json`.

### Hook structure (0–10 / 10–20 / 20–30)

The timed sentences must fill three thirds:

| Third | Job | Marker |
|---|---|---|
| 0–10s | Proof — the exact rank / number / beat the title promises | `[HOOK]` on the opening sentence |
| 10–20s | Stakes — what is threatened, what it costs to fail | a stakes sentence, or `[BEAT]` |
| 20–30s | Roadmap + open loop — what this part covers, one unanswered question | `[LOOP: the question]` |

Markers go on their own line before the sentence they belong to (or
inline: `He walks on. [LOOP: will he survive?]`). Missing markers log a
warning — they never stop the build.

The hook gets **no chapter cards** and **at most one callout** (the
planner's callouts are capped; a 30-second video cannot carry more).

## Manual intro (unchanged)

```bash
python -m manhwa_ocr video intro --prepare --session "Your Series Name"
# fill intro/intro.json: one entry per sentence, 1-3 panels like "ch2:P010-14"
python -m manhwa_ocr video intro --session "Your Series Name"
# -> video/chapter_0.mp4
```

Manual mode still reads `intro.json` only. Framing fixes for either mode
go in `video/chapter_0.fixes.json`, using the same `ch2:P010-14` ids.

## The 30-second rule

Target 30s, configurable 20–45s:

```yaml
# manhwa-ocr.yaml, video: section
hook: {target_seconds: 30.0, min_seconds: 20.0, max_seconds: 45.0, seed: 7}
```

Or per run: `video hook --duration 25 ...` (clamped into 20–45s).
Audio longer than the cap — or shorter than the floor — logs a warning.
The tool never trims audio to fit: re-record a tighter narration.

## Memory: `video/intro_memory.json`

After every successful hook/previously build, the tool records which
panels and beats were used, which loops were left open, and (for
previously) when the last bridge ran. The next auto-pick **avoids**
those panels unless the script marks the sentence `[REUSE]`:

```
He maxed every stat. [REUSE]
```

`--allow-reuse` allows reuse for the whole run. A picked panel that was
already used in intro + previously logs a warning. `series_memory.json`
is only ever **read** (characters / facts / open threads) — it is never
rewritten by hook/previously. `intro_memory.json` is a deliverable, not
scratch: it lives in `video/` next to the plans.

## Troubleshooting

- `No drawable panels in the given chapters` — every panel in range is
  text-only / credits, or the chapters have no `panels.json` yet. Run
  `video panels --chapter N` for each chapter first.
- `No panel left to pick ... mark a sentence [REUSE]` — the pool is
  exhausted (many sentences, few panels, memory exclusions). Add
  chapters, shorten the script, or mark a sentence `[REUSE]`.
- `The narration is too short to give every panel its minimum time` —
  fewer panels per sentence (auto-pick uses one) or a longer narration.
