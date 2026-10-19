# Editor guide — top-class editor reference (P0–P5)

What the editor does to every video, and the knobs that change it. This
doc is a reference, not a tutorial — the workflows are in `MANUAL_WAY.md`
(by hand), `AGENT_WAY.md` (prompts A–G), `HYBRID_WAY.md` (split).

## Pacing + motion (P3)

- **Panel hold:** every panel shows ~3–4s (5–7s only if it carries
  readable text). No panel >8s without a crop change or chapter card.
  Shots >8s are split into (4, 8]s pieces, each with an 8%-per-piece
  punch-in crop change. Split pieces respect the `min_hold` floor.
- **Motion:** continuous Ken Burns on every shot — never a frozen frame.
  Planner never emits `hold`. Motions are push_in (100→115%), pull_out
  (118→100%), and 4-way pans, varied so the same motion never repeats.
  Hand-set `hold` survives only for deliberate payoff beats.
- **Transitions:** hard cuts by default. Fade/dissolve only for time
  skips, flashbacks, scene changes, chapter boundaries. Whip/flash only
  for impact moments (≤1 per 3–4 min).

## Callouts (P3)

One keyword callout per impact/reveal shot: rank, level, number,
time-skip, or place. ≤40 chars, ≤2 lines, bottom banner, yellow for the
single keyword, never over the top 2/3 of the frame (face avoidance).
Override one shot's callout text in `fixes.json`:

```json
{"version": 1, "callouts": [{"shot_id": "S007", "text": "SS-RANK"}]}
```

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
flash, ding on system/status-window sentences. Render mixes them from
`video.sfx_dir`. Files needed in that folder: `whoosh.mp3`,
`impact.mp3`, `ding.mp3`, `sting.mp3`. Missing dir/files → one warning,
renders without SFX, never fails. No SFX ships with the program.

## Music (P3)

`music: null` means: explicit path wins, else `music/bed.mp3` in the
series folder when present, else music-free. Default volume 0.22
(15–25% under narration); sidechain ducking lets it rise toward 60–80%
when no narration speaks. Fade in/out 0.5s (≥0.3s).

## Hook + open loops (P3)

- First 30 seconds: proof (the exact rank/number/beat the title
  promises) + stakes + one-line roadmap. No greeting, no logo bumper.
- Open a loop every 2–3 min (`[LOOP: the question]`). End on an open
  question/cliffhanger. Biggest payoff at ~2/3, not in the last 30s.
- `[HOOK]` / `[LOOP: ...]` are whole-line markers in the recap; they
  attach to the next sentence, are never spoken, never appear in
  `tts.txt`. `plan["hook"]` carries explicit-hook + open-loop count +
  a warning when missing (never blocks).

## Crop / face safety (P1 + P2)

- A face is never masked. Mask-risk bubbles (dominant bubble overlapping
  the head) are never painted; crops steer around them. If no clean crop
  exists, verify DROPs the panel with a re-pair reason.
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
| `sfx_dir` | `null` (= no SFX) | Folder with whoosh/impact/ding/sting.mp3 |
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
