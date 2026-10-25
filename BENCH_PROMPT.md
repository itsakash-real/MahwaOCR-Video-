# Benchmark prompt — paste this whole file into your Antigravity agent

Both folders sit side by side in one workspace:

- `C:\Users\akash\Documents\manhwaOCR` — the MAIN project. **Read-only for you. Never touch it.**
- `C:\Users\akash\Documents\manhwaOCR_sandbox` — the upgrade sandbox (branch `perf/ai-hybrid-upgrade`). ALL work happens here.

---

```
Read AGENTS.md inside C:\Users\akash\Documents\manhwaOCR_sandbox first. You are benchmarking an
upgrade that is ALREADY IMPLEMENTED AND COMMITTED in that sandbox folder. You only RUN and REPORT —
never edit any code, config, or test in manhwa_ocr\ or tests\, never run git commit/merge, and never
modify anything in C:\Users\akash\Documents\manhwaOCR or C:\Users\akash\Documents\ManhwaRecaps
(reading the one baseline file named below is allowed).

GOAL: run one full chapter through the sandbox's upgraded OCR pipeline in PROVIDER mode, measure
it against a 10-20 minute SLA (target < 10), audit the transcript quality, run the test suite,
and write a report file I can hand back to the orchestrator.

SETUP (do this first, in the terminal your commands will run in):
1. cd C:\Users\akash\Documents\manhwaOCR_sandbox   (every command runs from here — this matters,
   it makes `import manhwa_ocr` resolve to the sandbox copy, not the main folder)
2. Set the API keys as environment variables ONLY. NEVER write them into any file, log, or report:
   PowerShell:
     $env:GEMINI_API_KEY="<paste gemini key>"
     $env:OPENROUTER_API_KEY="<paste openrouter key>"
   (Git Bash: export GEMINI_API_KEY="..." / export OPENROUTER_API_KEY="...")
3. $env:MANHWA_SESSIONS_ROOT="C:\Users\akash\Documents\manhwaOCR_sandbox\_e2e_sessions"
   (keeps every session inside the sandbox; the real ManhwaRecaps stays untouched)

BENCHMARK RUN:
1. Record the wall-clock start time.
2. Run exactly:
   python -m manhwa_ocr start "A Regressor's Tale of Cultivation" "https://example.com/chapter/1"
   python -m manhwa_ocr run --chapter 1 --folder _e2e/pages --session "A Regressor's Tale of Cultivation"
   (14 real pages are already staged at _e2e/pages; a session with this name may already exist with
   ~44 provider-read tiles in its cache — that is the designed resume behavior, keep going.)
3. IF THE RUN PRINTS [HYBRID-FALLBACK] (a key tripped its breaker mid-run): this is not a crash.
   a. Note the error and the time; wait 2 minutes (Retry-After), then re-run the SAME command —
      completed tiles are cached and kept, only the remainder is retried. Do up to 3 attempts.
   b. If Gemini keeps failing (daily quota), unset GEMINI_API_KEY for your terminal and re-run —
      kind:auto will route to OpenRouter automatically.
   c. If BOTH providers cannot finish the chapter, finish it yourself through agent mode: run
      `python -m manhwa_ocr next --session "A Regressor's Tale of Cultivation"` and do what it
      says (read the TASK_k.md shard tile images YOURSELF — you have vision — and write the
      tile_results_shard_k.json files exactly as the task files describe, running shards in
      parallel where you can), then `assemble --chapter 1`, answer .work/ch1/ASSEMBLE.md into
      .work/ch1/assembled.json yourself, then `finalize --chapter 1`. LABEL the run
      "agent-completed" in the report.
4. On success the command writes chapter_1.md plus .work/timing/chapter_1.json. Record the
   wall-clock total and copy the timing report's JSON into your report file.

QUALITY AUDIT (against the pre-existing baseline transcript — read-only):
   C:\Users\akash\Documents\ManhwaRecaps\a-regressor-s-tale-of-cultivation\chapter_1.md
1. Completeness: every tile id in .work/ch1/manifest.json has a result; report the counts.
2. Ground-truth spot checks (these were settled by earlier blind reads — verify the NEW
   transcript agrees):
   - The chapter contains a blank title-card panel with exactly "I AM AN EARTHLING, AN
     INHABITANT OF A CIVILIZED WORLD." (the old baseline MISSED this panel).
   - Around "AND WOKE UP IN A STRANGE WORLD." the art shows a giant three-tailed fox / an
     orange-eyed snake — NOT any "man in a dark bodysuit over a futuristic city" (that was a
     hallucination in one earlier pass).
   - No benches, no "UPGRADE BENCH", no system windows anywhere (another earlier hallucination).
3. Dedup: overlapping slice text must not produce repeated dialogue lines; scan the transcript
   for identical consecutive lines.
4. Korean: SFX like 쿨럭 / 두둥 / 스윽 must appear in Korean, not translated or deleted.
5. Note (do not fix) whether the transcript says FIFTY or FORTY years — earlier passes disagreed;
   quote whichever lines carry the number.
6. Overall: read the new transcript top to bottom; does it tell one coherent story matching the
   baseline's events (dying old man -> dumpling girl -> landslide/fox/snake/phoenix -> sages take
   the talented coworkers -> Director Kim leaves -> wells/soap/thieves -> ruin -> collapse ->
   wakes young -> Emperor crash -> Jeon Myeonghoon blame -> punch -> "what I DO have" ending)?
   List any event present in one but not the other.

TEST SUITE:
   python -m pytest tests/ -q --deselect tests/test_audit.py::test_code_guard_blocks_a_modified_program
   Expect all pass plus possibly 1 skipped. Report the exact tail line. If anything fails, capture
   the failure names — do NOT fix anything.

REPORT — write C:\Users\akash\Documents\manhwaOCR_sandbox\_e2e\BENCH_REPORT.md with exactly these
sections, every number from an actual command or file (never estimated):
   1. Verdict table: GATE 1 (total wall clock vs 10-20 min SLA) and GATE 2 (quality audit pass or
      the specific failures), PASS/FAIL each.
   2. Timing: wall clock per stage (prepare/slice, tile reading, assembly, finalize) +
      .work/timing/chapter_1.json verbatim + which provider finished the reads + how many tiles
      came from cache vs fresh + every [HYBRID-FALLBACK] event with its cause and time.
   3. Payload: (optional but nice) tile PNG size vs the compressed size the provider sends — run:
      python -c "from pathlib import Path; from manhwa_ocr.images import encode_image; t=sorted(Path('_e2e_sessions/a-regressor-s-tale-of-cultivation/.work/ch1/tiles').glob('*.png'))[5]; import base64; d,m=encode_image(t); print(t.name, t.stat().st_size//1024, 'KB png ->', len(base64.b64decode(d))//1024, 'KB', m)"
   4. Quality: each audit item above with PASS/FAIL and a one-line evidence quote.
   5. A 10-15 line verbatim sample of the new transcript (the opening).
   6. Suite result line.
   7. Anything confusing or broken you hit, with the exact command + error.
Then STOP and summarize the verdict table in your final message. Do not merge, do not edit code,
do not delete anything, never print or store the API keys.
```
