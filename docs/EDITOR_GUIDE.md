# Editor guide — top-class editor reference (P0–P5)

What the editor does to every video, and the knobs that change it. This
doc is a reference, not a tutorial — the workflows are in `MANUAL_WAY.md`
(by hand), `AGENT_WAY.md` (prompts A–G), `HYBRID_WAY.md` (split).
CapCut-style edits + the video-defect bugfixes live in `CAPCUT_EDITS.md`.

## Pacing + motion (P3)

- **Panel hold:** every panel shows ~3–4s (5–7s only if it carries
  readable text). No panel >8s without a crop change or chapter card.
  Shots >8s are split into (4, 8]s pieces, each with an 8%-per-piece
  punch-in crop change. Split pieces respect the `min_hold` floor.
- **Motion:** continuous Ken Burns on every shot — never a frozen frame.
  Planner never emits `hold`. Motions are push_in (100→115%), pull_out
  (118→100%), and 4-way pans, varied so the same motion never repeats.
  Hand-set `hold` survives only for deliberate payoff beats. Never pan
  twice in a row, never repeat the previous motion; split pieces
  alternate motion and only the first piece arrives with the shot's
  transition. See `CAPCUT_EDITS.md` (defects 1–2).
- **Transitions:** hard cuts by default (90%), fade 10%. `slide` / `zoom`
  / `whip` / `glitch` only on impact/reveal beats; glitch (0.2s RGB split)
  at most once per 60s. Joins are frame-exact, concat is a hard cut by
  design — no crossfade. See `CAPCUT_EDITS.md`.

## Callouts (P3)

One keyword callout per impact/reveal shot: rank, level, number,
time-skip, or place. ≤40 chars, ≤2 lines, bottom banner, yellow for the
single keyword, never over the top 2/3 of the frame (face avoidance).
Override one shot's callout text in `fixes.json`:

```json
{"version": 1, "callouts": [{"shot_id": "S007", "text": "SS-RANK"}]}
```

## Auto-captions (CapCut)

Plan `captions`: each shot's sentence split into 3–5 word chunks, spread
evenly across the shot, burned in at render from
`video/chapter_N.captions.ass` (ffmpeg subtitles filter, final pass —
captions never touch the segment cache). Bottom centre, key word
(longest word) yellow, black outline, never over faces. Full text;
callouts stay keyword-only, so the two never double up.
`video.captions: true` by default for new builds; old plans render
without captions unless requested:

```bash
python -m manhwa_ocr video captions --chapter N --session "Your Series Name"
# -> adds captions to the plan + writes video/chapter_N.captions.ass
```

A subtitles-filter failure keeps the un-captioned video and logs a
warning, never fails the render.

## Chapter cards (P3 + P5)

Videos ≥6 min get a card every ~6 min at a shot boundary: 1.75s,
full-screen dark overlay, auto title from the boundary sentence
(max 40 chars). Cards feed YouTube timestamps via
`chapter_cards_to_timestamps()`. Override a title in `fixes.json`:

```json
{"version": 1, "chapter_cards": [{"shot_id": "S012", "title": "The Exam That Kills"}]}
```

## SFX (P3)

Plan `sfx` events: whoosh on chapter cards, impact on shake, sting on
  flash, ding on system/status-window sentences. CapCut extras: scratch
  on twist reveals, rumble on long (>5s) reveals, hit on impact
  sentences with no shake of their own. Render mixes them from
`video.sfx_dir`. Files needed in that folder: `whoosh.mp3`,
`impact.mp3`, `ding.mp3`, `sting.mp3`, `scratch.mp3`, `rumble.mp3`,
`hit.mp3`. Missing dir/files → one warning,
renders without SFX, never fails. No SFX ships with the program.

## Music (P3)

`music: null` means: explicit path wins, else `music/bed.mp3` in the
series folder when present, else music-free. Default volume 0.22
(15–25% under narration); sidechain ducking lets it rise toward 60–80%
when no narration speaks. Fade in/out 0.5s (≥0.3s).

**Beat sync:** `video.beats` (list of seconds) or `music/beats.json`
(`{"beats": [...]}`) snaps interior cuts to the nearest beat within
±0.15s — first/last cuts never move, no shot under 0.2s. No beats =
no-op, cuts stay exactly on their sentences.

## Hook + open loops (P3)

- First 30 seconds: proof (the exact rank/number/beat the title
  promises) + stakes + one-line roadmap. No greeting, no logo bumper.
- Open a loop every 2–3 min (`[LOOP: the question]`). End on an open
  question/cliffhanger. Biggest payoff at ~2/3, not in the last 30s.
- `[HOOK]` / `[LOOP: ...]` are whole-line markers in the recap; they
  attach to the next sentence, are never spoken, never appear in
  `tts.txt`. `plan["hook"]` carries explicit-hook + open-loop count +
  a warning when missing (never blocks).

## Hook video + previously (30s each)

- `video hook --chapters 1-6 --audio audio/chapter_0.mp3 --script intro/intro.txt`
  → `video/chapter_0.mp4`. `video intro --auto` is the same build.
  `video previously --chapters 1-6 --audio audio/previously_7.mp3
  --script previously/previously_7.txt` → `video/chapter_prev7.mp4`.
- Hook structure is the same rule as above, compressed: 0–10s proof
  (`[HOOK]`), 10–20s stakes, 20–30s roadmap + `[LOOP: ...]`. Missing
  markers warn, never block. No chapter cards, at most one callout.
- Auto-pick: lexical overlap with panel OCR text first, face/impact
  pixel heuristics on top (portrait ≈ close-up, wide ≈ establishing,
  low-dialogue ≈ clean impact). Never text-only/credits, never a repeat,
  story order never backwards, tease ceiling keeps payoff panels out
  until the narration names the beat. Picks + reasons land in
  `intro/intro.auto.json` / `previously/previously_prev7.auto.json`.
- Previously: beats from the previous part's **recaps** (never
  transcripts), panels ONLY from the given chapters (no next-part leak
  by construction), preference for panels already shown in those
  chapters' videos. Threads used for beat selection are cut off at the
  previous part — later-chapter threads never steer it.
- Memory: `video/intro_memory.json` records used panels / beats / open
  loops / last previously run. Later picks avoid them unless the
  sentence is marked `[REUSE]` (or `--allow-reuse`). `series_memory.json`
  is read-only here.
- 30-second rule: target 30s, 20–45s via `video.hook` config or
  `--duration`. Over/under logs a warning; audio is never trimmed.

## Crop / face safety (P1 + P2)

- A face is never masked. Bubble painting was removed ENTIRELY:
  `clean_bubbles()` returns the panel unchanged (pixel-identical) --
  there is no flag to re-enable painting. Bubbles stay visible, so
  crops steer around them (bubble scoring); a panel whose best crop
  still shows a bubble is DROPPED and re-paired (the P1 mask-risk
  rule, now applied to every bubble). Mask-risk bubbles (dominant
  bubble overlapping the head) are dropped the same way. See
  `CAPCUT_EDITS.md` (defect 3).
- Crop QA checks every layout (single / pair / wide). A crop clean in
  single but not in pair is `layout_sensitive`; the planner auto-switches
  to the pair-safe shape (e.g. 4:3 → 3:4) and logs the switch in
  `crop_stats["layout_switches"]` + QA. No switch happens when the pair
  layout has no clean crop — that stays a reviewer decision.
- Visible erased-bubble patches are flagged via `patch_visibility`
  (texture-contrast ≥0.12) as a reviewer warning, never a drop reason.

## Sync (P2)

One gate code path (`evaluate_gate` / `gate_from_report`) shared by audit
and render. Cached audits are reused only under the current sync
settings. `{same}` across a story jump fails validation (name the
skipped panel, retag). Timing warnings ride with plan + QA and the
estimated-timing render refusal names the exact warning code + fix.

## Safety (P0)

No silent fallbacks: every estimated-timing path records a structured
warning (`code` / `message` / `fix`) surfaced in `video status` via
`timing_warnings`. Corrupt cues fail loudly (`load_cues_strict`) instead
of falling back to even timing. `pairs.json` / `cues.json` validation
fails the whole file with line-level errors. Alignment + plan are
deterministic (same inputs → identical JSON).

## Config knobs (`manhwa-ocr.yaml`, `video:` section)

| Knob | Default | What it does |
|---|---|---|
| `format` | `16:9` | `16:9` YouTube, `9:16` Shorts |
| `fps` | 30 | Frames per second |
| `crf` / `preset` | 20 / `veryfast` | Quality / speed |
| `min_hold` | 3.0s | No picture stays up less than this |
| `min_shot` / `max_shot` / `target_shot` | 1.0 / 5.0 / 2.7s | Shot-length pacing |
| `pair_ratio` | 0.3 | Share of even-timed shots shown as two cards |
| `effects` | `{shake: true, punch: false, flash: false, end_fade: true}` | Which impact effects are on |
| `music` | `null` (= `music/bed.mp3` if present) | Music bed path |
| `music_volume` | 0.22 | 15–25% under narration |
| `music_fade` | 0.5s | Fade in/out (≥0.3s) |
| `sfx_dir` | `null` (= no SFX) | Folder with whoosh/impact/ding/sting/scratch/rumble/hit.mp3 |
| `captions` | `true` | Auto-captions on new builds (3–5 words, key word yellow) |
| `grade` | `null` (off) | `"subtle"` = contrast +5%, saturation +5% |
| `beats` | `null` (off) | Beat times (seconds) to snap cuts to, ±0.15s; or `music/beats.json` |
| `crop.full_width` | `true` | Cards keep full width, slide vertical window |
| `whisper_model` | `base.en` | Use `small` for Hindi narration |
| `require_review` | `false` | If true, render refuses unless plan approved |
| `sync.gate_wrong` / `gate_window` / `gate_grace` | 0.05 / 0.25 / 0.2 | Sync-gate thresholds |

## `fixes.json` overrides (all in one file, `video/chapter_N.fixes.json`)

```json
{"version": 1,
 "drop": [{"panel": "P016-06", "reason": "sound-effect lettering only"}],
 "face": [{"panel": "P017-02", "top": 0.12, "bottom": 0.46}],
 "crop": [{"panel": "P018-05", "shape": "3:4", "box": [0.0, 0.10, 0.6, 0.85]}],
 "shot": [{"panel": "P019-01", "background": "black", "enter": "fade_in"}],
 "chapter_cards": [{"shot_id": "S012", "title": "The Exam That Kills"}],
 "callouts": [{"shot_id": "S007", "text": "SS-RANK"}]}
```

Validated as a whole: one bad entry refuses the file. Unknown shot_id in
`chapter_cards` / `callouts` = logged warning in `override_warnings`,
never a failure. Re-run `video plan --force` after any edit, then
`video verify` again.

## `video status` editor signals (P5 `editor` dict)

`timing_warnings` (code + fix) · `mask_risk` count · `layout_switches`
count · `hook` (explicit yes/no + open-loop count) · `chapter_cards`
count · `callouts` count · `sfx` (events + files present/missing) ·
`music` (file + present/missing) · `override_warnings`.
