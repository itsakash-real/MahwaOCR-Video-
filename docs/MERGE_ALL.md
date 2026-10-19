# Merge-all — one integration branch with everything

## Result

**Branch:** `editor/harden-all` — HEAD `9b146d0` (see "Later merge"
below). It contains the original P0–P5 chain plus the
Intro/Previously merge (`1c88bb2`) plus the CapCut-edits bugfix merge
(`8422248`) plus the Ch10 test-build fixes (`9b146d0`).

**Merged into `main`:** merge commit `f0e7d4c`
(`git merge --no-ff editor/harden-all`). `main` had zero commits
outside `editor/harden-all` (merge-base = old `main` HEAD
`95403d8`), so the merge was clean with no conflicts and dropped
nothing. Full suite on merged `main`: **630 passed, 1 skipped,
1 deselected**. PR #3 (`perf/video-p0-double-work` → `main`) is
superseded by this merge — its commits are inside the chain — and
must be closed, not merged.

## Later merges (after the original P0–P5 + docs)

### Ch10 test-build fixes — commit `9b146d0`, merged into `main` as `f0e7d4c`

Fixes for issues found in the Ch10 new-code test build
(`test-ch10-newcode`, 255.67s, 65 shots, 83 sentences), all directly
on `editor/harden-all`:

- **Caption timing:** captions were chunked per-shot, leaving 1–5s
  gaps during narration and captioning sentence 41's tail ~5s early.
  Captions are now built from alignment segments (sentence windows)
  and validated (chunk inside its sentence window ±0.5s, no
  narration gap >1s). Test rebuild: zero caption gaps >1s, sent-41
  tail chunk at 127.29–128.71 (on time), sync gate PASS, wrong-picture
  0.8%.
- **Caption bar:** captions sat on the card and covered panel text
  (241s: "BY MUTAKTA!"). Captions now render in a dedicated black
  bottom bar (bottom 8%); cards are capped to the top 92%.
  Spot-check: 241s caption in the bar, panel text fully visible.
- **Panel diversity:** same panel 3x consecutively with an identical
  crop (55–57 P014-10, 75–77 P020-01). The third showing is now
  punched in until its crop differs from both predecessors
  (`diversity_changes` in the plan). Spot-check: 174s third showing
  is a distinct tighter crop.
- **P2 wide-switch guard:** `find_pair_safe_shape` only switches to
  shapes valid for the target layout (wide: never 1.0); stale cache
  entries are validated on read; `verify.json` labels its switches
  as single-layout so a `1.777 -> 1.0` entry is never misread as a
  wide-layout switch. Test rebuild: planner recorded zero layout
  switches.
- **System-window wide fill:** the ~34s system-notification panel
  (P004-07) rendered as a small square. System-window banners are
  split out of pair units and get their own wide shot with
  `wide_fill` (≥90% frame width). Spot-check: 34s banner fills the
  frame, "INEXPLICABLE MENTAL FORTITUDE!" legible.
- **Filler stretches (sent 4, sent 68) left as-is:** both fillers
  are story-correct; no cleaner distinct panel exists (sync-audit
  flags them `[filler_panel]`, gate still PASS).
- **File size:** test rebuild 47 MB vs original 33 MB (+38%) —
  expected cost of continuous motion + captions, not a defect.
  Do not lower quality / change CRF to chase size.

Tests: new `tests/test_caption_and_layout_fixes.py` (23 tests).
Full suite on `editor/harden-all` before merge: **630 passed,
1 skipped, 1 deselected**.

### Intro + Previously — merge `1c88bb2`

`video hook` / `video intro --auto` (auto-pick panels from across
chapters for a 30-sec hook) + `video previously` (30-sec bridge from
previous part's recaps only), memory in `video/intro_memory.json`
(no repeat panels, `[REUSE]` override), local + deterministic.
Tests after merge: 574 passed, 1 skipped.

### CapCut edits + defect fixes + bubble-painting removal — merge `8422248`

Feature branch `editor/capcut-edits-bugfix` (6 commits, `8e9b80c` …
`8991dd3`), merged with `git merge --no-ff` as `8422248`.
What it fixes (all three defects reported in Ch1–10 videos, built on
old code `cbaed57`):

- **Defect 1 — 0.5s glitch on panels held >3s:** old code eased Ken
  Burns over `KB_MAX = 2.6s` then held, cached one static frame
  (`static_from`), and joined segments with `-c:v copy` (no
  crossfade). Fix: continuous motion for the whole shot, no static
  freeze for long shots, frame-exact joins, and never split a long
  sentence into multiple shots with the SAME panel without a crop
  change.
- **Defect 2 — left-right swap repeating >3 times in 8s:** old
  planner picked motion independently per shot (`hold 55 / push_in
  30 / pan 15`), so a split same-panel stretch could re-pick pan
  and restart `_viewport` from scratch, plus `PAIR_MOVE = 0.7`
  re-triggered on every pair. Fix: planner never repeats the
  previous motion, never picks pan after pan unless the crop
  changes, pair_move at most once per pair shot.
- **Defect 3 — faces cut + colour on faces:** `clean_bubbles()`
  painting removed **entirely** (painting code deleted, not flagged
  off). `clean_bubbles()` is now a no-op returning the panel
  unchanged, pixel-identical. Bubble *detection*
  (`bubble_regions` / `bubble_boxes`) stays and still drives crop
  scoring, drop decisions, and QA. Panels with no clean crop are
  DROPPED and re-paired (P1 mask-risk rule, now applied to every
  bubble that would have been painted). Cleaned-panel disk cache
  key versioned (`v: 2`) so old painted cache entries are never
  served again.

CapCut-style edits added (tasteful, recap-appropriate): transitions
(hard cut default, fade, slide, zoom, whip, glitch 0.2s RGB split on
impact/reveal beats only, max 1 per 60s), continuous keyframe-style
Ken Burns, auto-captions (ASS, 3–5 words at a time, key word yellow,
bottom band, `video.captions` config, default ON for new builds),
glitch effect, beat sync (cuts snap to nearest beat within ±0.15s
when music exists), SFX additions (record-scratch, rumble, hit),
optional gentle grade (`video.grade: subtle`, default OFF).
Deliberately NOT added: stickers, emojis, speed-ramp spam. See
`docs/CAPCUT_EDITS.md`.

Tests after merge: **608 passed, 1 skipped, 1 deselected**
(pre-existing root guard test).

Why no merge commits: P0–P5 were built as a linear chain on top of
`perf/remaining-audit` (`cbaed57`), which already contains yesterday's
PR1–PR4. The chain already *is* the merge-all. This branch is the named,
verified pointer at that chain — created with `git worktree add ...
-b editor/harden-all editor/harden-p5-ux-shorts`. No branch pointer was
moved; `perf/remaining-audit`, `main`, and every phase branch are
untouched.

## What is in the chain (verified by `git log` + ancestry)

Base `cbaed57` — `perf/remaining-audit` — contains yesterday's edits:
PR1 (`492f12e`, verify no-replan, opencv dep, audio-timing cache, x264
threads, vignette/shadow cache, LRU caches, sync-audit reuse) +
PR2 (`4737010`, per-panel crop cache + cleaned-panel disk cache) +
PR3 (`f603a34`, transcript/recap: `prepare --shards`, vision batch,
vectorized gutter, PNG fast paths, parallel downloads/slicing) +
PR4 (`cbaed57`, `pair --autofix`, pairing thumb LRU, shared download
cache, segment-cache key, `plan/build --no-storyboard`,
`_lexical_overlap` pre-tokenize, syncaudit gate linear sweep, full
scene text in recap listing). All five verified as ancestors of HEAD.

On top, in order (16 commits, `cbaed57..HEAD` = 32 files, +3881/−135):

| Phase | Commits | Branch |
|---|---|---|
| P0+P1 safety + crop/face | `78df967`, `71a9a5f`, `b186502` | `editor/harden-p0-p1` |
| P2 sync | `bf48ec9` | `editor/harden-p2-sync` |
| P3 editor layer | `21c9d62`, `56fc050`, `00fde83`, `a9665fd`, `f771b27` | `editor/harden-p3-editor` |
| P4 perf | `c046690`, `fa671e4`, `b389d71` | `editor/harden-p4-perf` |
| P5 UX/Shorts | `6df039c`, `d175346`, `5e5d718`, `41f0442` | `editor/harden-p5-ux-shorts` |
| Docs (this branch) | 1 docs commit | `editor/harden-all` |

Branch graph (linear, oldest → newest):

```
origin/main (95403d8)
  └─ prod-backup (c98b85e) ── perf/remaining-audit (cbaed57)  [PR1–PR4]
       └─ editor/harden-p0-p1 (b186502)  [P0+P1]
            └─ editor/harden-p2-sync (bf48ec9)  [P2]
                 └─ editor/harden-p3-editor (f771b27)  [P3]
                      └─ editor/harden-p4-perf (b389d71)  [P4]
                           └─ editor/harden-p5-ux-shorts (41f0442)  [P5]
                                └─ editor/harden-all (41f0442 + docs)  [THIS]
```

`origin/main..HEAD` diff: 62 files, +11247/−736 (that includes the
whole PR1–PR4 perf diff, because `main` predates it — see PR strategy
below).

## Verification (before + after)

- **Full suite on P5 HEAD (pre-merge):** 552 passed, 1 skipped,
  1 deselected (`test_code_guard_blocks_a_modified_program` — pre-existing
  root failure: `os.access` reports read-only files as writable for root;
  proven on clean base). Exit 0.
- **Full suite on `editor/harden-all` after docs commit:** re-run before
  pushing (docs-only change; expected identical).
- **Smoke:** `python -m manhwa_ocr --help` and `video --help` run clean;
  `video` actions include `shorts` and `endscreen`.
- **No duplicate definitions:** no module-level duplicate `def` in
  `manhwa_ocr/` (same-name methods in different classes — e.g. Provider
  subclasses — are expected).
- **Imports:** `editor`, `shorts`, `plan`, `frames`, `render`, `build`,
  `verify`, `syncaudit`, `crops` all import clean.
- **Main checkout:** `~/workspace/MahwaOCR-Video-` still on
  `perf/remaining-audit` `cbaed57`, clean, untouched.
- **`main` / `prod-backup`:** read-only checks only. Never checked out,
  never modified, never force-pushed. No history rewritten.

## PR strategy (do NOT merge to `main` here)

From `docs/HARDENING_PR_PLAN.md` (P4): stacked PRs chained off
`perf/remaining-audit`, each showing only its own phase, merge in
order with normal non-force merges:

| PR | Head → Base | Shows |
|---|---|---|
| PR-H1 | `editor/harden-p0-p1` → `perf/remaining-audit` | P0+P1 only |
| PR-H2 | `editor/harden-p2-sync` → `editor/harden-p0-p1` | P2 only |
| PR-H3 | `editor/harden-p3-editor` → `editor/harden-p2-sync` | P3 only |
| PR-H4 | `editor/harden-p4-perf` → `editor/harden-p3-editor` | P4 only |
| PR-H5 | `editor/harden-p5-ux-shorts` → `editor/harden-p4-perf` | P5 only |
| PR-H6 | `editor/harden-all` → `editor/harden-p5-ux-shorts` | Docs only |

Then merge the chain into `perf/remaining-audit` first, then that
branch → `main` once via PR. Close PR #3
(`perf/video-p0-double-work` → `main`) as superseded — its commits are
inside the chain — do NOT merge PR #3 separately. Do NOT create PRs or
merge anything from this task; the branches are pushed, the PRs are
for the user to open.

Note: PR-H6 (`editor/harden-all` → `editor/harden-p5-ux-shorts`)
now shows docs + the Intro/Previously merge + the CapCut-edits
bugfix merge, since `editor/harden-all` has moved past the original
docs commit. If a per-phase PR is wanted for the bugfix, open it as
`editor/capcut-edits-bugfix` → `editor/harden-all` at `1c88bb2`
(the pre-merge base) and merge it before PR-H6.

## What is in `main` vs not in `main`

- **AGENT_WAY update (after the `main` merge):** prompts A–H rewritten to
  the current workflow (CapCut fixes, caption timing + caption bar,
  panel diversity, P2 wide-switch guard, system-window wide fill,
  bubble-painting removal, review + rebuild procedure as new Prompt H).
  See `docs/AGENT_WAY.md`.
- **In `main` (merge `f0e7d4c`):** everything below — the older
  alignment/tag-wins + README work (old HEAD `95403d8`), PR1–PR4,
  P0–P5, Intro/Previously, CapCut edits + bugfixes, and the Ch10
  test-build fixes (`9b146d0`). `main` is now the integration
  branch; `editor/harden-all` remains as the feature branch pointer
  at `9b146d0`.
- **NOT in `main`:** nothing from this chain. `perf/remaining-audit`
  and the phase branches are untouched.
