# Previously guide — the 30-second "Previously on..." bridge

Between parts, a 30-second bridge reminds viewers what happened — without
spoiling what comes next. It outputs `video/chapter_prevN.mp4`, where N is
the next part's first chapter (chapters 1–6 → `chapter_prev7.mp4`).

**You supply all audio.** The tool never creates, trims, or generates
audio. Record the bridge narration first, then build.

## Build

```bash
# 1. Write the narration: one sentence per line, 5-8 beats.
cat > previously/previously_7.txt <<'EOF'
Previously, Minwoo maxed every stat at the level 1 cap of 50.
His father wired him 200 billion won.
He enhanced Summon Skeleton to plus 9.
Then the Death Lord's voice spoke from the trial gate.
[LOOP: will he survive the trial?]
EOF

# 2. Record it as audio/previously_7.mp3 (30 seconds, same rule as the hook).

# 3. Build: panels auto-picked from chapters 1-6 ONLY.
python -m manhwa_ocr video previously --chapters 1-6 --audio audio/previously_7.mp3 \
    --script previously/previously_7.txt --session "Your Series Name"
# -> video/chapter_prev7.mp4
```

`video previously --dry-run` plans without rendering.
Defaults (no `--script` / `--audio`): script
`previously/previously_7.txt`, audio `audio/previously_7.mp3`.

## Pin the panels yourself (cues file)

Auto-pick is a heuristic: one panel per sentence, chosen by OCR overlap.
When a bridge has to land specific pictures — the character's face in the
sentence that names them — pass a cues file instead of a script. Each
sentence may pin 1–3 panels with refs exactly as in the manual intro
(`ch2:P010-14`):

```bash
cat > previously/previously_7.cues.json <<'EOF'
{"sentences": [
  {"text": "Welcome back to part two of this episode.",
   "panels": ["ch2:P002-01"], "hook": true},
  {"text": "So the ancient monsters adopted it in chat: a slime queen, a lich, and a wolf.",
   "panels": ["ch1:P012-09", "ch2:P002-04", "ch2:P026-08"]}
]}
EOF
python -m manhwa_ocr video previously --chapters 1-6 --audio audio/previously_7.mp3 \
    --cues previously/previously_7.cues.json --session "Your Series Name"
```

Pinning is all-or-nothing: if any sentence pins panels, every sentence
must pin 1–3, each pin must be a drawable panel of the `--chapters` pool,
and no panel may be pinned twice — anything else is an error, never a
silent fall-back to auto-pick for the rest. The `.auto.json` review file
is still written, with `"source": "panels pinned in cues file"`. The same
pins work for `video hook --cues`.

## Where the panels come from

- **Beats come from the previous part's recaps** (`recaps/chapter_N.md`
  for the given chapters) — up to 8 key beats, evenly spaced across the
  part, preferring sentences that name an open thread from
  `series_memory.json`. Full transcripts are never used, and the next
  part's chapters are never read.
- **Panels come ONLY from the given chapters.** The pool is built from
  `--chapters` and nothing else, so a next-part panel cannot leak in —
  by construction, not by a filter applied afterwards.
- **Panels that already appeared in those chapters' videos are
  preferred** (read from each chapter's `video/chapter_N.plan.json`),
  so viewers see the pictures they already associate with the beats.
- The same auto-pick rules as the hook apply: lexical overlap first,
  face/impact pixel heuristics on top, never text-only/credits, never a
  repeat, story order never backwards, payoff guard on.
- Auto-pick writes `previously/previously_prev7.auto.json`: the picked
  panels + reasons, plus the recap beats they illustrate. Review it
  like the hook's auto file.

## No-spoiler rules

- `--chapters` must list **only the previous part**. If the next part
  starts at chapter 7, pass `--chapters 1-6`, never `1-7`.
- The bridge never reads `series_memory.json` facts learned after the
  previous part: open threads used for beat selection are the ones open
  at that point. (The memory read is read-only; hook/previously never
  rewrite `series_memory.json`.)
- The bridge gets **no chapter cards** and **at most one callout**,
  like the hook.

## The 30-second rule

Same as the hook: target 30s, configurable 20–45s via
`video.hook` in config or `--duration` (clamped). Audio over the cap
or under the floor logs a warning; the tool never trims audio.

## Memory: `video/intro_memory.json`

The bridge records its panels / beats into `video/intro_memory.json`
under `previously`, plus `last_previously_timestamp` and
`last_previously_for` (the chapters it bridged). The next hook or
bridge avoids those panels unless the script marks the sentence
`[REUSE]` (or `--allow-reuse` is passed). See `INTRO_HOOK_GUIDE.md`
for the full memory format.

## Troubleshooting

- `video previously needs --chapters` — pass the previous part, e.g.
  `--chapters 1-6`.
- `No script file ... previously/previously_7.txt` — write the script
  (one sentence per line) or pass `--script`.
- No recap files for the given chapters — the bridge still builds from
  your script alone, but beat steering is skipped; write the recaps
  first for the best picks.
