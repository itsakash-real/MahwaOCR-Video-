# The recap system — map, incident verdicts, and refinement notes

Written 2026-10-24 on branch `refine/recap-system`, from a full read of the
recap path plus empirical probes of the live code. Every file:line below was
verified against the working tree at commit time; every measured number was
produced by a command (see "Evidence commands" at the bottom).

Part 1 maps the system as it stood before this branch. Part 2 gives the
verdict for each incident in the incident log. Part 3 records the corpus
situation on this machine (what could and could not be validated against real
data). Part 4 lists the refinements this branch ships and where each lives.

---

## Part 1 — The map (as found on `integration/transcription-speed`)

### 1.1 The flow, end to end

```
chapters.txt ──transcribe──> chapter_N.md ──┐
series_memory.json (bible) ─────────────────┤
                                            v
  recap --prepare   manhwa_ocr/script.py RecapBuilder.prepare (script.py:972)
      writes .work/recap/{plan.json, TASK.md, prompts/chN.md}
  [the WRITING AGENT reads TASK.md + prompts + contact sheets]
      writes .work/recap/parts.json  {"N": {narration, hook, summary}}
  recap --finalize  RecapBuilder.finalize (script.py:1071)
      gates: cues.check (video/cues.py:277) + script_lint (per lane)
      writes recaps/chapter_N.md (+ recap-meta), .cues.json, .tts.txt
      deletes .work/recap/ on full success (script.py:1121 -> cancel:962)
```

There is deliberately no LLM wiring in the recap stage: provider mode exists
(`generate`, script.py:881) but the production path for this repo is agent
mode — a human/agent writer answers the prepared prompts into `parts.json`.

CLI entry: `cmd_recap` (cli.py:348-384); `--minutes` defaults to 6.0
(cli.py:1195), i.e. a 900-word target at `WORDS_PER_MINUTE = 150`
(script.py:49). There is one global length target; no per-lane word band
exists anywhere in code (see incident 7).

### 1.2 What prepare loads per lane (and what it does not)

`_prompt_for` (script.py:728-767) assembles, per style `recap.style`
(config.py:287-307; current global setting: `manhwabro`):

| Input | default | speedrun | powerfantasy | manhwabro |
|---|---|---|---|---|
| Series bible (`memory.as_briefing`) | as of chapter N (`upto`) | as of chapter N | WHOLE range, no cutoff (script.py:735) | as of chapter N (script.py:748) |
| Range beat-digest block (`_range_block`, script.py:666-695) | — | — | yes (700 chars/chapter, 6000 total) | — |
| Previous segment: story-so-far + previous ending (`_previous_block`, script.py:597-622) | yes | yes | yes | yes |
| Panel list + contact sheets (`_panel_block`, script.py:639-647) | yes | yes | yes | yes |
| Chapter transcript | full | full | full | full |
| Scope note (range opener vs continuation) | — | — | yes (script.py:697-703) | yes |

What prepare does NOT load, for any lane:

- Panel usability beyond `text_only`/`credits` — no bubble/head geometry, no
  mask-risk (that is computed at `video plan` time from the detector; the QA
  file with `mask_risk` is written by video/build.py:278). See incident 9.
- Any record of what context the writer actually saw: the assembled prompt
  files under `.work/recap/prompts/` are deleted on successful finalize
  (script.py:1121 -> cancel() at 962-970 rmtree). See incident 11.
- Any verification that the range's transcripts exist, except for
  powerfantasy (`_select`, script.py:772-787). Other lanes check only the
  chapters actually requested (script.py:788-794). See incident 3.

### 1.3 The gate inventory (what each gate actually measures)

**cues.check** (video/cues.py:277-343) — runs for every lane at finalize
(script.py:1100-1101) and in provider mode (script.py:917):

- ERRORS (chapter not saved): a sentence tag pointing at a `credits` or
  `text_only` panel (cues.py:302-311), with the nearest drawn panel named.
- WARNINGS (logged only): `tagged_ratio < 0.90` (cues.py:315-317); a run of
  ≥3 sentences under 6 words (cues.py:318-329); digits present (330-336);
  fewer than ~min_hold·wpm/60 words per drawn panel (337-342).

**script_lint** (manhwa_ocr/script_lint.py) — pattern checks, run only for
powerfantasy and manhwabro (script.py:713-726). **There is no lint_default
and no lint_speedrun** — the default and speedrun lanes have no style gate
of any kind.

- `lint_powerfantasy` (script_lint.py:114-197): opener-only `hook-formula`
  (regex for "This guy is ..., a ... at ..., but..."), `lore-drop` (exact
  phrase), `past-life-opening` (banned markers in first 3 sentences); every
  chapter: `finale-ending`, `restart-opening` (phrase lists),
  `camera-cue`/`camera-direction`, `cue-form` (title-card detectable).
- `lint_manhwabro` (script_lint.py:230-270): delegates the every-chapter PF
  gates, adds opener `bro-pivot` (a "So, why ...?" regex within 14
  sentences), opener `past-life-opening`, and `bro-epithet` (>= 1 epithet
  match per chapter, BRO_MIN_EPITHETS = 1).

**video/tts_lint.py** — a SEPARATE linter with a SEPARATE contract, enforced
only later, at `video tts` (Deepgram synthesis, tts.py:198-200 refuses the
whole chapter): `digits`, `dash-pause` (any spaced em/en dash or `--`),
`ellipsis`, `double-punct`, `unterminated`, `quotes`, `run-on` (>30 words).
Nothing at recap time runs tts_lint or shares its punctuation contract
(incident 6).

What NO gate measures, for any lane: events per sentence (chaining),
concrete power claims (ranks/numbers/named techniques), witness-reaction
beats, persona density beyond the single manhwabro epithet, pivot/aside
density, factual agreement with the transcript, join quality between
chapters beyond the banned-phrase lists (incidents 1, 7, 10).

### 1.4 How cues, tags and {same} are produced and counted

- Tags are `{P003-02}` / `{same}` / `{beat,impact,reveal}` tokens read by
  `cues.parse` (cues.py:148-249). `SENTENCE_END = (?<=[.!?…])["”’)\]]*\s+`
  (cues.py:50) splits sentences. Abbreviations are protected
  (cues.py:115-125). A quoted `?`/`!` is protected ONLY when it is the last
  terminator before the closing quote AND the next word is lowercase
  (`_QUOTE_CONTINUATION`, cues.py:130-145; added by commit deeb147 "F4",
  2026-10-21).
- `[HOOK]` / `[LOOP: ...]` whole-line markers are interleaved onto the next
  non-empty line as `\x00`-tokens (cues.py:161-175) and stripped before
  text is derived (cues.py:193-194). tts.txt additionally strips whole
  bracket lines and inline brackets (`tts_text` cues.py:104-112 +
  `_spoken` 59-65).
- `tagged_ratio` = sentences with at least one non-`same` panel ÷ all
  sentences (cues.py:234-235, 247). A sentence tagged only `{same}` gets
  `same: true, panels: []` (cues.py:236-237) and counts as UNTAGGED in the
  ratio. Tag-only fragments merge into the next sentence (cues.py:221-233).
- The ratio is written into recap-meta (`tagged`, script.py:859) and is the
  number `check()` warns on.

### 1.5 Every read and write of the bible, across all modules

Writes (merge + save):
- `pipeline.py:342-344` — provider-mode transcript finalize.
- `agentflow.py:537-539` — agent-mode transcript finalize (from the
  `memory_update` field of `assembled.json`).
- `cli.py:599-621` — `memory reset` (backup + remove; user command).

Reads:
- `script.py:512` — RecapBuilder init (recap side; READ ONLY — incident 2).
- `pipeline.py:86`, `agentflow.py:79` — transcript side init.
- `status.py:52-56` — `next`/`status` briefing.
- `cli.py:201, 567` — session list/info summaries.
- `video/align_engine.py:564-566` — name hints for alignment.
- `video/hook.py:540-543` — intro auto-pick context (read-only by design).

### 1.6 Where each style's rules exist twice (doc vs code), and agreement

| Lane | Code home | Doc home | Drift status at branch start |
|---|---|---|---|
| default | `SCRIPT_SYSTEM` (script.py:56) | no `RECAP_STYLE_DEFAULT.md` exists; partial rules in `docs/manhwa_ocr_transcription_and_recap_guide.md` and AGENT_WAY Prompt B (incl. third-person rule) | doc set incomplete; no lint exists; third-person rule enforced nowhere in code (incident 8's original bug class) |
| speedrun | `SPEEDRUN_SYSTEM` (script.py:72) | `docs/RECAP_STYLE_SPEEDRUN.md` | doc's rules match the system text; NO lint exists for the lane; doc does not claim one |
| powerfantasy | `POWERFANTASY_SYSTEM` (verbatim, script.py:97) + `POWERFANTASY_PROMPT` | `docs/RECAP_STYLE_POWERFANTASY.md` | agree (doc §5 gate list == script_lint codes; range gate doc §2 == script.py:772-787) |
| manhwabro | `MANHWABRO_SYSTEM` (script.py:136) + `MANHWABRO_PROMPT` | `docs/RECAP_STYLE_MANHWABRO.md` | agree (doc gate list == lint_manhwabro codes) |

Nothing MECHANICAL checks doc↔code agreement (incident 8); the two style
docs that exist were hand-verified for this map and agreed at branch start.

### 1.7 Artifacts a finished chapter leaves behind

- Transcript stage: `chapter_N.md`, `media/chN/pages/`,
  `media/chN/tiles.json`, updated `series_memory.json` (+ `.prev.json`),
  `.work/timing/chapter_N.json`; `.work/chN/` scratch deleted.
- Recap stage: `recaps/chapter_N.md` (with `recap-meta`: chapter, words,
  target_minutes, hook, summary, tagged, written_at),
  `recaps/chapter_N.cues.json`, `recaps/chapter_N.tts.txt`, optional
  `recaps/bookends.json`; `.work/recap/` (TASK.md, plan.json, prompts/)
  DELETED on success — the brief is not a persisted artifact (incident 11).
- Video stage (later): panels.json (built at recap time if pages exist),
  alignment/plan/qa/fixes/video files under `video/`.

---

## Part 2 — Incident verdicts (1-11)

Verdict key: LIVE = reproducible on the branch-start code; FIXED-BY = the
named pre-branch commit; PARTLY = both; rank: (a) ships a bad script
silently, (b) breaks continuity, (c) wastes downstream work.

| # | Incident | Root cause (file:line) | Verdict | Rank | Fix class |
|---|---|---|---|---|---|
| 1 | Lint-pass scripts read badly (rejected powerfantasy pass) | The gate inventory (1.3) measures form only; no gate measures chaining, concreteness, reactions, persona density. The one persona gate (`bro-epithet`) needs a single match (script_lint.py:227). | LIVE (system-level) | (a) | new quality-signal report (4.4) + warnings; persona qualities that cannot be mechanized stay review steps |
| 2 | Bible never written by recap side | script.py:512 loads the bible; no `merge`/`save` anywhere in script.py; writers are pipeline.py:342-344 / agentflow.py:537-539 only | LIVE | (b) | data flow: recap finalize merges a memory_update through `SeriesMemory` (same writer path) |
| 3 | Transcripts-first is a promise, not a mechanism | Range gate exists ONLY for powerfantasy (script.py:772-787); manhwabro (current flagship lane) and others check only requested chapters (788-794); nothing reports the context the writer had | PARTLY (PF enforced since 704b403; not the other lanes) | (b) | contract: prepare enforces the range for range-style lanes + context manifest |
| 4 | Splitter breaks tags inside quotes; {same} miscounted | `SENTENCE_END` (cues.py:50) splits at interior `?`/`!` inside a quoted span whenever the next word is capitalised — the deeb147 "F4" protection (cues.py:130-145) covers only quote-final terminators followed by lowercase. Probe: `{P001-01} He shouts "What is this? Is this the end?" and collapses.` → untagged fragment. `{same}`-only sentences count as untagged in `tagged_ratio` (cues.py:234-235) and carry `panels: []` (236-237) | PARTLY (F4 fixed one shape) — interior-quote and {same} counting LIVE | (c) | tooling: quote-interior protection + honest ratio incl. `same`; refuse orphaned sentences |
| 5 | LOOP-marker leak into cues | `SENTENCE_END`'s trailing class `["”’)\]]*` includes `]`, so the `?] ` at a `[LOOP: ...?]` cue's end splits THERE: the cue is decapitated, its literal text survives `_spoken` (bracket no longer closed), and the following sentence's tag sticks to the leak. tts.txt strips closed brackets so it looks clean — but `video tts` speaks cues.json sentences (tts.py:68-81), so the leak is narrated. Reproduced in probe D2/D3; LIVE IN PRODUCTION: chronicles-of-the-demon-slayer chapters 1-5+ shipped cues.json each carry a leaked `[LOOP: ...` pseudo-sentence, ch1's even carrying `panels: ['P014-01']` | LIVE (production data) | (a)+(c) | tooling: protect bracket spans in the splitter + finalize guard refusing literal cue text |
| 6 | Conflicting punctuation regimes | Speedrun/default style text uses spaced em-dashes (RECAP_STYLE_SPEEDRUN.md examples; 17 dash-pause hits in monster ch1, 19 in lumberjack ch1 — real files), while tts_lint `_DASH_PAUSE` (tts_lint.py:24) hard-refuses them at `video tts` (tts.py:198-200). Recap finalize runs no tts_lint | LIVE | (a)+(c) | contract: shared punctuation gate surfaced in the finalize report (warning), no style text changed |
| 7 | Length control is soft | One global `--minutes` (default 6, cli.py:1195) × 150 wpm; words only warned when too FEW for the panel count (cues.py:337-342); no per-lane band, no upper bound, no event-density measure; meta records words but nothing refuses | LIVE | (a) | new gate-warning in quality report: words vs lane band + events-per-sentence density |
| 8 | Spec/code drift between docs and script.py | Style rules live in script.py constants; specs in docs/RECAP_STYLE_*.md; no test compares them. (The doc-named third-person bug class: AGENT_WAY Prompt B line "Third person only" is enforced by no gate today.) | LIVE (no mechanism); no current disagreement found by hand | (b) | new sync test over checkable claims (gate codes, constants, markers) |
| 9 | Downstream-blind tagging | Recap-time panel metadata is panels.json's `text_only`/`credits` (errors exist for those, cues.py:302-311, plus [TEXT ONLY] hints in the prompt listing, script.py:400-407). Mask-risk needs detector maps computed only at `video plan` (crops.py:1152-1174; recorded in QA json, build.py:278) — unknown at recap time | PARTLY (text-only handled; mask-risk blind) | (c) | warning at finalize: text-heavy drawn panels + any mask-risk panels already recorded in the session's QA file |
| 10 | No factual audit | Nothing compares a finished recap sentence to its transcript; the grounding rule lives only in prompts (script.py systems; agent_prompts RECAP_RULES) | LIVE | (a) | new claim-check report: entity/event grounding per sentence, saved with the chapter |
| 11 | One-shot regeneration impossible | prepare writes the full per-chapter prompts to `.work/recap/prompts/chN.md` (script.py:999-1000) — then successful finalize DELETES the whole task dir (script.py:1121 -> cancel() 962-970). The brief existed and was destroyed | LIVE | (c) | data flow: persist a dated BRIEF artifact next to the recap before cleanup |

Corrections to the incident log, from the code: (i) `lint_default` /
`lint_speedrun` do not exist — only powerfantasy and manhwabro have style
lint; (ii) incident 4's "coverage overstates" is imprecise: `{same}`-only
sentences UNDERSTATE `tagged_ratio`; the real overstatement is incident 5's
leak pseudo-sentence carrying a panel id (counted as tagged while being
garbage text); (iii) incident 4's quote-split is already half-fixed
(deeb147, 2026-10-21) for the quote-final + lowercase shape only.

---

## Part 3 — Corpus situation on this machine (validation honesty)

- The powerfantasy lane was committed 2026-10-23 (704b403), manhwabro
  2026-10-24 (ffbf3bc). EVERY recap on this machine was written 2026-10-07
  .. 2026-10-22 (recap-meta `written_at`), i.e. the rejected powerfantasy
  pass and any manhwabro recap DO NOT EXIST on this disk, and there is no
  "Solo Swordmaster" session (checked `session list` + a full-text search
  of the sessions root; the repo root only holds a build PROMPT for it).
- Consequence, stated plainly: quality signals could not be validated
  against the actual rejected scripts. Validation instead uses (a) all real
  recaps on disk (9 series, 3210 cue sentences measured), and (b) a
  constructed negative control built from the incident log's own post-mortem
  description (one event per sentence, generic claims, no reactions, no
  persona) applied to a real chapter — labelled as a control, never as the
  rejected pass. Where a number came from the control rather than a real
  file, the report says so.
- The flagship-lane demonstration runs on a real series with a real
  transcript: "A Regressor's Tale of Cultivation" chapter 1, written through
  the refined flow in the manhwabro lane (the lane the config selects), and
  the report side-by-side pairs it with the constructed rejected-profile
  control of the same chapter (same facts, opposite craft).

## Part 4 — What this branch changes

See `git log` on `refine/recap-system` and the sections below; each commit
is small and test-covered. No style system text (SCRIPT/SPEEDRUN/
POWERFANTASY/MANHWABRO systems or prompts) was modified; where a fix would
have required touching a style prompt, it is listed in the final report as
a proposal instead.

### Evidence commands (Part 1/2 numbers)

- Splitter/LOOP probes: `python -` heredoc over `cues.parse`/`cues.tts_text`
  with the nine shapes quoted in tests/test_recap_refine.py (cases A-F).
- Production sweep: parse of every `recaps/chapter_*.cues.json` under the
  sessions root → 3210 sentences, 305 untagged non-same, 57 same-only,
  5 literal-`[` leaks (chronicles ch1-5), each leak printed with its text.
- tts_lint on real chapter 1 cues: monster 23 issues (17 dash-pause),
  lumberjack 35 (19 dash-pause), chronicles 0.
- Baseline suite before any edit: 707 passed, 1 failed
  (tests/test_titlecards.py::test_title_card_render_keeps_the_audio_continuous
  — the test shells out to bare `ffmpeg`, absent from this machine's PATH;
  the repo's own resolver would have found the imageio-ffmpeg binary), plus
  the known test_audit env failure.
