# AI upgrade benchmark report — perf/ai-hybrid-upgrade (sandbox)

Run date: 2026-10-24/25. Everything below happened inside the sandbox
worktree `../manhwaOCR_sandbox` (branch `perf/ai-hybrid-upgrade`); the main
project directory was never modified by the upgrade work (its uncommitted
edits at report time are the user's own parallel work).

## Verdict up front

- **GATE 1 (speed): FAIL** in the only mode this machine can run without a
  vision API key — measured 9 s for prepare+slice+handoff, then **102.8 min
  of active tile reading** for the 122-tile chapter (plus a 149-min idle gap
  while the session was paused, excluded from the number). The 10-20 min SLA
  is a provider-mode target; the provider path was verified by unit tests
  but cannot be demonstrated live with zero keys.
- **GATE 2 (quality): FAIL** — the produced tile corpus contains
  independently-proven fabrications. Blind, context-free re-reads of two
  contested tiles show each pass hallucinating different panels (details
  below). The transcript therefore cannot be certified, and per the merge
  rule **nothing was merged into the main project directory**.

## What was implemented (all unit-tested, suite green in the sandbox)

| Spec item | Where | Verification |
|---|---|---|
| JPEG compression + downscale on encode | `images.encode_image(max_width=800, jpeg_quality=85)` | real page: 1290 KB PNG -> **116 KB JPEG (9%)**; unit tests incl. alpha flatten, no-upscale |
| Multi-key rotation, 429 cooldowns, circuit breaker | `providers.ResilientKeyPool` | unit tests: comma keys, pacing, breaker disable, 401 instant-disable, empty pool exhausts in **0 ms** |
| `kind: auto` provider chain + fallback_providers | `providers.build_provider` | resolves gemini -> openrouter -> nvidia -> openai -> anthropic by present env keys; endpoint defaults per kind |
| Keep-alive connection pooling | shared `requests.Session` (HTTP/1.1) | all provider calls reuse it; HTTP/2 would need a new dependency (documented, not added) |
| Hybrid circuit-breaker + agent fallback | `pipeline.Pipeline` | mid-run exhaustion preserves completed tiles in `tile_results.json`, shards ONLY the unresolved tiles via agentflow's own TASK renderer, loud actionable stop; `hybrid_agent_fallback: false` restores the old behavior; no-keys run shards everything instantly |
| Config (spec 3.1) | `manhwa-ocr.yaml` + DEFAULT_CONFIG | slicing 1800/220/800, provider timeout 90 / retries 3 / payload knobs, pipeline concurrency 6 / vision_batch 4 / agent_shard_tiles 10 / hybrid on / stall 5 |

Test suite in the sandbox: **1191 passed, 1 skipped, 1 deselected** (the
deselected one is the known pre-existing `test_audit` guard env failure) —
including 15 new `tests/test_hybrid_upgrade.py` tests and the two updated
contract tests.

## The E2E run (agent-fallback path, no vision API keys present)

Chapter: the 14 pages of "A Regressor's Tale of Cultivation" ch1 (copied
into the sandbox), baseline = the session's existing transcript
(.work/timing: 24.4 s prepare / 1286 s read / 105.4 s answer / **1448.1 s
total**, 102 tiles, 6 shards).

1. `run --chapter 1 --folder ...` — 14 pages -> **122 tiles** at the new
   slicing (1800/220/800), `kind: auto` exhausted in **< 1 s**, handoff wrote
   13 TASK shards in **9 s total**. Exactly the spec's Tier-3 path.
2. Tile reading ran with the platform's **3-concurrent-subagent cap** (a 4th
   spawn was rejected). 122 tiles took **102.8 min of active work**
   (~90-140 s per tile round-trip through the available vision tool; one
   worker stalled and 10 tiles were re-read hours later, then 10 more tiles
   were re-read once more — see quality findings).
3. Assemble was started, then halted when the quality audit below failed.

## GATE 1 numbers (all from file mtimes / command output)

| Stage | Baseline | This run |
|---|---|---|
| prepare (download/import + slice) | 24.4 s | **9 s** (local pages; slicing of 122 tiles) |
| provider exhaustion -> shards | n/a (hard crash path) | **< 1 s + 9 s** |
| read_tiles | 1286 s (102 tiles, 6 workers) | **6168 s active** (122 tiles, 3 workers) |
| total active | 1448.1 s | **~103 min** (read-dominated) |

Diagnosis: the pipeline-side work is now seconds; the read phase is bounded
by (tiles x per-tile vision latency) / worker-cap. On this box that is
122 x ~115 s / 3 ≈ 78 min, which matches. The SLA path is provider mode:
31 batched requests (vision_batch 4) across 6 workers at the measured
per-call latency ≈ 1-2 min + one assembly call. That arithmetic, not a live
run, is the honest speed claim — no vision key exists here to prove it.

## GATE 2 findings (the important part)

Three distinct quality failures were caught, each with evidence:

1. **Session-name anchoring (my benchmark setup's fault).** 10 tiles had
   bench/"UPGRADE BENCH"-themed content that matches the session name I
   chose ("Upgrade Bench"), not the story. Example: a "park bench / AND SO
   MY SECOND LIFE BEGAN" panel; the real page shows the Emperor appearing
   in the sky. Root cause: the per-tile prompt states the series name, and
   a wrong/meaningless name steers the vision backend. **Pipeline lesson:
   the session name must be the real series name, or the series-name line
   should be dropped from tile prompts.** (Fixed for the audit by a
   surgical 10-tile re-read under the corrected context.)
2. **Wholesale panel confabulation by the vision backend.** A blind,
   context-free re-read of tile p003_t03 (rubble + "AND WOKE UP IN A
   STRANGE WORLD." narration + purple-glow fox — matching the BASELINE)
   disproves this run's "man in a dark bodysuit over a futuristic city"
   reading of the same tile, dialogue included. Conversely the blind read
   of p002_t08 (a blank title-card panel with centered "I AM AN EARTHLING,
   AN INHABITANT OF A CIVILIZED WORLD.") proves the NEW read right and the
   BASELINE incomplete there. Conclusion: **both** passes fabricate
   individual panels; word-overlap metrics do not catch this (the invented
   text reuses the story's vocabulary).
3. **Numeric drift.** This run consistently reads "FIFTY YEARS" where the
   baseline reads "FORTY YEARS" for the same bubbles — unresolved without
   a decisive zoom re-read; flagged, not adjudicated.

Net: 0% missing tiles (122/122 covered, dedupe and Korean SFX handling
worked as designed at the merge level), but accuracy cannot be certified.
A transcript is only as trustworthy as its weakest tile read, and the
available vision path invents panels at a rate GATE 2 cannot accept.

## What is NOT merged and where everything lives

- Branch `perf/ai-hybrid-upgrade` in `../manhwaOCR_sandbox` (4 commits:
  the upgrade, its tests, two contract-test updates). Not merged anywhere.
- Benchmark sessions: `_e2e_sessions/upgrade-bench` (raw first pass) and
  `_e2e_sessions/regressor-bench-retake` (decontaminated pass, assemble
  halted) — kept for iteration; delete with the folder when done.
- Main project directory: untouched by this work; nothing was merged.

## Recommended next iterations (in order)

1. Put ONE real vision API key in the env (Gemini/OpenRouter) and re-run
   the same benchmark: the whole point of the upgrade is that provider
   mode then does the 122 tiles in minutes, and GATE 1/2 get a fair shot.
2. Blind reads: drop the series-name line from the per-tile prompt (or make
   the session name exact); the two blind probes were the only reads that
   were right on the first try in both directions.
3. If agent mode must stay the only path: add a blind second-pass
   verification of a random tile sample per shard (the audit that caught
   everything above), and re-shard at 10 tiles (already the new default)
   across as many workers as the platform allows.

---

## Addendum (2026-10-25, after keys were provided)

The user supplied a Gemini and an OpenRouter key. Findings so far, all inside the sandbox:

- Gemini key is VALID but `gemini-2.0-flash` is retired on the live API -> model id updated to
  `gemini-3.8-flash` (yaml + AUTO_KINDS, committed). A single-tile probe read the contested
  p002_t08 title card CORRECTLY (matches blind ground truth).
- Live provider runs then exposed two real transport issues, both fixed and unit-tested:
  1. Batched 4-image vision calls on slow backends exceed 90s -> provider `timeout` raised to
     240s (pipeline stall watchdog still bounds hangs).
  2. **OpenRouter 402 "in_flight_budget_exhausted"** (small-balance key + 6-way concurrency)
     carries a Retry-After and means "wait", not "key dead" -> `providers._keyed_post` now
     treats 402-with-Retry-After as a cooldown; bare 402 still disables. Regression test added.
- The hybrid machinery proved itself live: three mid-run key trips each PRESERVED the completed
  tiles (32 -> 44 across resumes), sharded only the remainder, and stopped cleanly in ~1 min.
  Remaining wall-clock blocker was purely key quota/credit behavior, not pipeline code.
- Next: the benchmark is being re-run externally (Antigravity agent, BENCH_PROMPT.md) with the
  keys set; its report lands at `_e2e/BENCH_REPORT.md` and decides the gates.

---

## External benchmark (user's Antigravity agent, 2026-10-25) — verified on disk

The user's agent ran the chapter in agent-completed mode (APIs skipped at the user's direction;
44 previously provider-read tiles came from cache + 78 read by the agent itself) and reported
PASS/PASS. Verification against the artifacts on disk:

- Every quality claim checks out EXCEPT its "narrative coherence without omissions" line — its
  transcript omits the baseline's soap/lye/thieves arc. Follow-up BLIND reads (p006_t03,
  p006_t04 — the exact tiles the baseline cites for the soap arc) show only "I'VE TRIED MY
  HARDEST TO LIVE." and the "I LEARNED THE LANGUAGE... / MADE A LIVING, / ...BUILT MY OWN
  HOUSE." narration — which the new transcript carries verbatim. NO soap, lye, market, or
  recipe text exists in the art: **the baseline transcript's soap arc was confabulated by the
  original 2026-10-23 agent run** (and the series bible inherited it). The new transcript also
  correctly says FIFTY years (the baseline's FORTY is a misread; both new passes + the raw
  Korean agree), and the celestial-voice / REGRESSION / 독원 panels are real (three independent
  readers now agree on those lines). Five independent blind reads across the chapter now agree
  with the new transcript wherever they were checked.
- Consequence flag for the REAL session: `ManhwaRecaps/a-regressor-s-tale-of-cultivation`
  chapter_1.md and series_memory.json contain invented content (soap business, thieves,
  forty-years) and should be regenerated or hand-corrected.
- GATE 2 (quality), agent-completed run: **PASS** — verified on disk; it is *better* than the
  baseline it was audited against.
- GATE 1 (speed), agent-completed run: **FAIL by the numbers** — the agent's own timing file
  shows read_tiles 4110.9 s (68.5 min) + answer_assembly 1131.8 s; its "PASS within workflow
  SLA" verdict is not defensible against the 10-20 min window, and the file's `total` (2885.5 s)
  is internally inconsistent with the stage sum (5246.4 s) because read_tiles spans cached
  history from earlier sessions.

## Provider-mode runs (the SLA path)

- Attempt 1 (Gemini, 6 workers, 90 s timeout): 32 tiles kept; healthy batch calls were killed
  by the 90 s read timeout on a slow backend -> timeout raised to 240 s (committed).
- Attempt 2 (Gemini resume): 44 tiles kept, then instant 429s (daily quota blown) -> hybrid
  handoff, clean stop.
- Attempt 3 (OpenRouter 72B, concurrency 6): tripped on 402 "in_flight_budget_exhausted"
  (small-balance key + big model reserve) — fixed the classification (402+Retry-After = cooldown,
  not key-death; regression-tested, committed).
- Attempt 4 (OpenRouter 72B, concurrency 3, 158 tiles due to a missing slicing block in the
  override): 16 tiles kept; the in-flight reserve still 402s at 72B concurrency, and the pool's
  120 s wait cap raced OpenRouter's 120 s Retry-After -> agent handoff.
- Attempt 5 (OpenRouter **qwen2.5-vl-7b-instruct**, concurrency 4, correct 122-tile slicing):
  result below.
