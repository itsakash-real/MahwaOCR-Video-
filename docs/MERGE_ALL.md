# Merge-all — one integration branch with everything

## Result

**Branch:** `editor/harden-all` — HEAD `41f0442`, identical tree to
`editor/harden-p5-ux-shorts` (no diff). No merge commits were needed.

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

## What is in `main` vs not in `main`

- **In `main` (`95403d8`):** the older alignment/tag-wins + README work.
  It does NOT contain PR1–PR4, P0–P5, or these docs.
- **NOT in `main` (on `editor/harden-all`):** everything above — all
  perf work, all hardening, all docs. Nothing in this task merged
  anything into `main`.
