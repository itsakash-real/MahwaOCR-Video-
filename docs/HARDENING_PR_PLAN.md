# Hardening PR plan — P0 → P1 → P2 → P3 → H-P4

Status: plan + code complete through H-P4. Nothing here is merged.
No PR in this plan has been created yet. Do NOT merge any of them
until the PR-4 decision in §4 is made.

Terminology (two different "P4"s exist — do not confuse them):
- **PR-1…PR-4** = the earlier perf PRs. PR-4 lives on branch
  `perf/remaining-audit` (commit `cbaed57`). PR #3
  (`perf/video-p0-double-work` → `main`) is already open on GitHub.
- **H-P0…H-P4** = this hardening stack (safety → crop/face → sync →
  editor → perf). H-P0+H-P1 share one branch; H-P2, H-P3, H-P4 each
  have their own.

## 1. What the stack is

All hardening branches fork from `cbaed57` (`perf/remaining-audit`,
which already contains PR-1…PR-4 perf work), NOT from `main`.
That is deliberate: the hardening work was built and tested on top
of the perf caches. Consequence: any hardening PR opened against
`main` shows PR-1…PR-4 perf diff *plus* hardening diff.

| Phase | Branch | Head | Diff vs its base | Tests (full suite) |
|---|---|---|---|---|
| H-P0+P1 (safety + crop/face) | `editor/harden-p0-p1` | `b186502` | 11 files, +812/−44 | 469 passed, 1 skipped |
| H-P2 (sync) | `editor/harden-p2-sync` | `bf48ec9` | 7 files, +769/−29 | 490 passed, 1 skipped |
| H-P3 (editor layer) | `editor/harden-p3-editor` | `f771b27` | 13 files, +1017/−51 | 517 passed, 1 skipped |
| H-P4 (perf) | `editor/harden-p4-perf` | *(this branch)* | frames.py + tests + this doc | see §3 |

Pushed so far: H-P0+P1, H-P2, H-P3 are on GitHub (`origin/...`).
H-P4 is local-only until the parent pushes it.
`main` checkout in the workspace is untouched at `perf/remaining-audit`
(`cbaed57`); `main` itself was never checked out or modified here.

## 2. Recommendation: stacked PRs chained off `perf/remaining-audit`

Open four small PRs, each showing ONLY its own phase:

| PR | Head → Base | Shows |
|---|---|---|
| PR-H1 | `editor/harden-p0-p1` → `perf/remaining-audit` | P0+P1 only |
| PR-H2 | `editor/harden-p2-sync` → `editor/harden-p0-p1` | P2 only |
| PR-H3 | `editor/harden-p3-editor` → `editor/harden-p2-sync` | P3 only |
| PR-H4 | `editor/harden-p4-perf` → `editor/harden-p3-editor` | H-P4 only |

Why stacked, not one big PR:
- Each diff is 7–13 files / 800–1,100 lines — reviewable in one pass.
  A single H-P4 → `main` PR would be ~3,500 lines mixing perf +
  four hardening phases; a problem in P3 would block P0+P1.
- The chain is sequential by construction (P2 assumes P1's
  `layout_sensitive` flag; P3 assumes P2's gate; H-P4 assumes P3's
  motion). Stacking matches the dependency order.
- Any phase can be sent back for rework without un-blocking the
  ones before it; landing H1 early banks P0+P1 safety even if P3
  needs another round.

Why base the first PR on `perf/remaining-audit` instead of `main`:
- It makes PR-H1 a pure P0+P1 diff. Based on `main`, PR-H1 would
  also carry the whole PR-1…PR-4 perf diff, hiding the hardening
  changes inside it.
- The chain then merges into `main` as one final step (§4).

Exact commands (after `gh auth` is `itsakash-real` + `gh auth setup-git`):

```bash
gh pr create --base perf/remaining-audit --head editor/harden-p0-p1 \
  --title "Harden P0+P1: safety lock + crop/face" \
  --body "P0: loud timing warnings, strict cues/pairs validation, determinism locks. P1: never mask a face when a clean crop exists (drop + re-pair), layout-sensitive QA, patch visibility flag, feathered bubble cleaning. Tests: 469 passed, 1 skipped."

gh pr create --base editor/harden-p0-p1 --head editor/harden-p2-sync \
  --title "Harden P2: sync — pair-safe auto-switch, one gate, timing to action, {same} fix" \
  --body "Auto-switch to pair-safe crops, one sync-gate code path, timing warnings surfaced with fixes, {same}-across-jump validation. Tests: 490 passed, 1 skipped."

gh pr create --base editor/harden-p2-sync --head editor/harden-p3-editor \
  --title "Harden P3: editor layer — continuous motion, callouts, chapter cards, SFX, hook markers" \
  --body "Continuous Ken Burns (no hold), 4-8s pacing split, keyword callouts, chapter cards, SFX mix, music bed default, [HOOK]/[LOOP] markers. Tests: 517 passed, 1 skipped."

gh pr create --base editor/harden-p3-editor --head editor/harden-p4-perf \
  --title "Harden P4: perf — frame-path caches, PR plan" \
  --body "Bg/src/font caches (pixel-identical), PR1-PR4 cache regression locks, PR strategy doc. See docs/HARDENING_PR_PLAN.md. Tests: <fill in final run>."
```

Merge order: PR-H1 → PR-H2 → PR-H3 → PR-H4, normal (non-force)
merges, one at a time, re-running the suite on each merge commit.
Never force-push `main`. Never click Merge on a PR whose suite is red.

### Alternative: single PR (not recommended)

Open `editor/harden-p4-perf` → `main` directly. One review, one
merge — this matches the user's standing preference for perf work
("one branch / single PR"). The cost is a ~3,500-line diff mixing
five concerns, and a single requested change anywhere restarts
review of everything. Use it only if review bandwidth is the
binding constraint.

## 3. H-P4 measured performance

Benchmark: synthetic chapter-shaped plan, 12 shots × 30 frames,
1920×1080, `ShotRenderer.frame_array` only (no ffmpeg encode).
Baseline = H-P3 code; After = H-P4 code. Two runs each.

| Workload | Before (H-P3) | After (H-P4) | Change |
|---|---|---|---|
| Shared panel across shots (real chapter shape: one panel spans several sentences) | 35.4–36.5 ms/frame | 30.3–31.4 ms/frame | **~13–15% faster** |
| Distinct panel per shot (nothing repeats) | ~35.3 ms/frame | ~34–37 ms/frame | ~0% (noise) |

What H-P4 caches (all pixel-identical, verified by
`test_frame_pixel_identical_with_and_without_cache`):
- Backdrop: cross-shot bg cache (same `bg_panel` reuses the blurred +
  vignetted 1080p array) + one cached PIL copy per renderer
  (replaces `Image.fromarray` per frame).
- Card source: LANCZOS resize of a card's crop cached by
  (panel, crop, pad_y, target size).
- Overlay font: `truetype()` load cached per size.

What H-P4 deliberately does NOT cache: the moving Ken Burns viewport
itself. Profile: `Image.transform` is 54% of frame time and is
inherent to P3's continuous motion — caching it would change pixels.
That is the honest ceiling: H-P4 recovers rebuild overhead, it does
not make motion free.

Regression locks added (`tests/test_p4_perf.py`, 9 tests): bg/src/font
cache behaviour + pixel identity, crop-cache round-trip + key
stability, vignette/shadow cache hits, `_load_page` LRU identity,
segment-key stability (PR-4's `_jobs` key unchanged by H-P4).

## 4. The PR-4 decision (do this before merging the chain into `main`)

`perf/remaining-audit` (`cbaed57`) is itself unmerged, and PR #3
(`perf/video-p0-double-work` → `main`, head `f603a34`) is open.
`cbaed57` descends from `f603a34`, so the hardening chain already
contains PR-1…PR-4's commits. Pick one:

- **Option A (recommended):** Merge the hardening chain
  (PR-H1…PR-H4) into `perf/remaining-audit` first, then merge
  `perf/remaining-audit` → `main` once. Close PR #3 as superseded
  (its commits are inside the chain) — do NOT merge PR #3
  separately, or the same commits land twice and the chain PRs
  show confusing already-merged diffs.
- **Option B:** Merge PR #3 first, then rebase the hardening chain
  onto the new `main` and re-run the suite. More churn, same
  endpoint. Only pick this if PR #3 must land on its own for
  reasons outside this plan.

Either way: `main` gets the perf + hardening work in exactly one
merge path, never two.

## 5. Pre-merge checklist (every PR in the chain)

- [ ] Full suite green in the branch worktree
      (`pytest tests/ -q -p no:cacheprovider`, 1 pre-existing guard
      test deselected — fails as root, unrelated).
- [ ] No session files touched (`~/workspace/ManhwaRecaps/` clean).
- [ ] No force-push anywhere; PR base/head match §2.
- [ ] H-P3 note: P0–P3 change planner output (motion, callouts,
      cards, auto-switch). Existing `plan.json` files for Ch1–6
      will re-plan differently. Do NOT re-render old videos from
      new plans without user approval; verify on a fresh test
      chapter and diff the plan first.
- [ ] H-P4 note: no SFX/music files ship — a series folder still
      needs user-supplied `music/bed.mp3` + `sfx/*.mp3` to hear
      those layers; missing files warn and render without them.
