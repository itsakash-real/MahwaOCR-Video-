# Full pipeline workflow — link to finished video, one chapter at a time

This is the canonical, single-source-of-truth workflow for taking a chapter
from "just a link in `chapters.txt`" all the way to a finished, rendered
video, with Deepgram doing the narration instead of a human recording.

It exists for the same reason `docs/CHAPTER_BUILD_WORKFLOW.md` exists: this is
a long job made of many small phases, run back-to-back across one
continuation session, and the phase order fades from working memory long
before `AGENTS.md`'s general house rules do. Re-reading this file is the fix,
not remembering it.

**Read this file in FULL before starting a chapter, and read it again before
starting each NEW chapter in the same session** — even if you built the
previous one a few messages ago. Do not rely on memory of it.

This file covers Stages 0-4 (link → verified narration audio). For Stage 5
(audio → finished video), it hands off to `docs/CHAPTER_BUILD_WORKFLOW.md` —
**read that file too, fresh, before starting Stage 5** — this file does not
duplicate it.

---

## Before anything else

- Follow `AGENTS.md` exactly. Work only through the CLI
  (`python -m manhwa_ocr <command>`). Never hand-edit pipeline JSON except the
  specific files each phase below names.
- Work on `video/wide-mode-v2` (or a branch off it). Never touch `main`;
  never merge without an explicit go-ahead.
- Always pass `--session "<SERIES>"` explicitly on every command.
- One chapter at a time, in order. Series memory and recap continuity are
  order-dependent — never jump ahead to a later chapter because its audio
  happens to be ready first.
- Make a todo list for this session (not this file) with one row per stage
  below, and tick it off as you go. If interrupted, that list — plus what's
  actually on disk — is how you resume without re-doing finished stages.
- **Deepgram key**: read from `DEEPGRAM_API_KEY` env var, else a local key
  file (default `~/.config/manhwa/deepgram.key`, one line, key only). Never
  ask the user to paste the key into chat, never write it into the session
  folder or a commit, never print it in a log or error message.

## Stage map (what's a CLI gate today vs. an agent judgment call)

| Stage | What | Backed by |
|---|---|---|
| 0 | Chapter link → transcript | `prepare`/`assemble`/`finalize` (CLI, deterministic + vision) |
| 1 | Transcript → recap draft | `recap --prepare`/`--finalize` (agent writes the prose) |
| 2 | Recap ↔ transcript consistency check | **agent judgment call** — no dedicated command yet |
| 3 | Narration voice pass | **agent judgment call** (tone/register — no mechanical check can judge this) |
| 3b | TTS-readiness lint | `video tts` runs this automatically and REFUSES the whole chapter if anything's flagged (digits, dash/ellipsis-as-pause, stacked punctuation, unterminated sentence, unbalanced quotes, run-on >30 words) — see `manhwa_ocr/video/tts_lint.py` |
| 4 | TTS synthesis | `video tts` (CLI) |
| 4b | Audio self-check | `video tts` runs this automatically after synthesis (`--no-verify` to skip): re-transcribes the audio with the same whisper engine `video align` uses and flags any sentence whose heard words don't match the script, or whose seconds-per-word is a duration outlier (truncation/corruption) — see `manhwa_ocr/video/tts_verify.py`. Flags are a "go listen to this one" signal, not proof of a bug; whisper mishears real speech sometimes too. |
| 5 | Audio → finished video | `video pair/align/plan/verify/sync-audit/approve/render` (CLI gates) — see `CHAPTER_BUILD_WORKFLOW.md` |

Stage 2 and Stage 3 (the voice pass) have no dedicated command — they are not
optional just because there's no command to run. Treat each as a real gate:
do the check, and don't move to the next stage until it passes. If you find
yourself wanting to skip stage 2 or 3 "because the recap looks fine," that
instinct is exactly the failure mode this file exists to close off — name
ONE missing/wrong fact and ONE harsh-sounding sentence you fixed per chapter
you've reviewed this way; if you can't, you didn't actually look. Stages 3b
and 4b ARE mechanical now (lint + whisper re-transcription) precisely because
"does this sentence have raw digits in it" and "does the audio roughly match
the script" don't need judgment — only the voice/tone call in Stage 3 does.

---

## Stage 0 — Transcript

If there's no provider API key configured, use agent mode (read
`AGENTS.md`'s "Agent mode" section). Otherwise:

```bash
python -m manhwa_ocr run --next --session "<SERIES>"
```

Confirm with `python -m manhwa_ocr status --session "<SERIES>"` that
`chapter_N.md` now exists before moving on.

## Stage 1 — Recap draft

```bash
python -m manhwa_ocr recap --session "<SERIES>" --chapters N --prepare
#   read the TASK.md it prints, write .work/recap/parts.json
python -m manhwa_ocr recap --session "<SERIES>" --finalize
```

Follow `AGENTS.md`'s "Writing recaps" section exactly, including panel
tagging. Do not skip the tagging checklist there (≥90% sentences tagged, no
text-only/credits tags, no 3+ run of sub-6-word sentences, ~7.5+ words per
drawn panel) — Stage 5's gates assume it was followed.

**Always write the `"summary"` field in `parts.json`**, not just `narration`
and `hook`. This is the one thing that lets a brand-new session (no memory of
the chapters built before it) continue the story correctly -- it's read back
into every later chapter's prompt as the "story so far." A thin or skipped
summary is exactly how a fresh session loses track of an open thread (a
reveal, a hidden identity, a promise) from a chapter it never saw being
written.

## Stage 2 — Recap ↔ transcript consistency check

Before touching tone or TTS formatting, verify the recap didn't drift from
the source:

1. List every named entity (character names, place names, named objects/
   techniques) and every concrete fact (numbers, relationships, stated
   outcomes) that appears in `recaps/chapter_N.md`.
2. For each, confirm it appears in `chapter_N.md` (the transcript) with the
   same spelling/value. A name that's spelled two different ways between
   transcript and recap is exactly the kind of thing `series_memory.json`
   should already have normalized — if it didn't, flag it, don't silently
   pick one.
3. For anything you can't confirm from the transcript text alone (e.g. the
   recap describes a visual detail — an expression, an object in the
   background — the transcript's dialogue-focused text doesn't capture),
   check it against the actual panel image before accepting it as fact. This
   is the "also check in the image" step — only needed when the transcript
   text genuinely doesn't settle it, not on every sentence.
4. Fix only the mismatches found (edit the recap's `.md`, not the
   transcript). Do not rewrite sentences that already check out.
5. Report what you checked and what you fixed, even if the answer is
   "nothing was wrong" — a silent pass is indistinguishable from a skipped
   check, so say which entities/facts you verified.

## Stage 3 — Narration voice + TTS-readiness pass

Two different concerns, both before synthesis, because fixing voice/wording
after audio exists means re-recording (or here, re-synthesizing and
re-aligning):

**Voice pass** — read the recap aloud (mentally) against this rubric: gentle,
young, engaging narrator — warm and a little informal, not a stiff
documentary reader, not a hype-trailer voice. This is on top of, not instead
of, whatever house style/tone the series' own recap config already specifies
(check it — don't assume a single global tone fits every series). Rewrite any
sentence that reads flat, overly formal, or mismatched in register from its
neighbors. Never touch a chapter that already has audio
(`audio/chapter_N.*`) unless asked.

**TTS-readiness pass** — this is now enforced by `video tts` itself, not a
manual read-through: it lints `recaps/chapter_N.cues.json` before spending
any Deepgram call and refuses the WHOLE chapter if anything's flagged
(raw digits, a dash/ellipsis standing in for a sentence break, stacked
punctuation, an unterminated sentence, unbalanced quotes, a run-on sentence
over ~30 words). You'll see every issue at once (same "fix everything, then
retry once" pattern as the align-failure-loop rule in
`CHAPTER_BUILD_WORKFLOW.md`), each with the sentence number and a suggested
fix. Edit the recap's `.md` (not `cues.json` directly) and re-run
`recap --finalize` so the two stay in sync, then re-run `video tts`.
Panel tags (`{P003-02}` etc.) are untouched by this pass — the finalize step
already strips them before `cues.json` is written.

## Stage 4 — Synthesize narration, then verify the audio

```bash
python -m manhwa_ocr video tts --chapter N --session "<SERIES>"
```

This reads `recaps/chapter_N.cues.json` (never `.tts.txt`), lints it (Stage
3b, above), synthesizes each sentence via Deepgram, writes
`audio/chapter_N.m4a` plus `audio/chapter_N.tts.json` (voice used,
per-sentence timings), then automatically runs the Stage-4b audio self-check
and writes `audio/chapter_N.tts_verify.json`. Re-running after a Stage-3 edit
only re-synthesizes the sentences that changed (cache keyed by
`model + text`, and a cache entry that fails to decode — e.g. an interrupted
write — is treated as a miss, never trusted blindly); `--force` redoes
everything.

**If `video tts` reports flagged sentences** (from the automatic self-check,
or from a standalone `video tts-verify --chapter N` re-check):
1. Read `audio/chapter_N.tts_verify.json` — each flag names the sentence
   number, what was expected vs. what whisper actually heard, and why
   (low word-match ratio, or a seconds-per-word outlier suggesting
   truncation/corruption).
2. Listen to just the flagged sentence(s) — this is a targeted check now,
   not a full-file listen, because the self-check already narrowed it down.
   A flag CAN be a false positive (whisper mishears real speech sometimes
   too); confirm before treating it as a defect.
3. If it's a real defect, the fix is almost always back in Stage 3 (reword
   the offending sentence so Aura reads it cleanly) — not a code change.
   Re-run `recap --finalize` then `video tts` (only the changed sentence
   re-synthesizes).
4. Zero flags, or every flag confirmed as a whisper false positive: move to
   Stage 5.

**Code issues:** if you hit a genuine bug in `tts.py` or elsewhere, stop, fix
it on `video/wide-mode-v2` (or a branch off it), run
`python -m pytest tests/ -q`, commit and push, then continue (pre-approved;
merging to `main` is not).

## Stage 5 — Audio → finished video

**Read `docs/CHAPTER_BUILD_WORKFLOW.md` in full now**, even if you just read
this file — it is a separate document with its own phase numbering (its
Phase 1 through Delivery), and covers `video pair/align/plan/verify/
sync-audit/approve/render` in the detail this file doesn't repeat. Treat the
Stage-4 output (`audio/chapter_N.m4a`) exactly as that file treats a
human-recorded `audio/chapter_N.mp3` — nothing downstream distinguishes them.

---

## Multiple chapters in one request

Run Stages 0-5 **separately, in full, per chapter** — do not skip stages for
later chapters because they feel redundant, and re-read both this file and
`CHAPTER_BUILD_WORKFLOW.md` again before starting the next chapter's Stage 0,
even if you just finished the previous chapter's Stage 5 a moment ago.

## Delivery

Report, per chapter: what Stage 2 found (verified facts / fixed mismatches),
what Stage 3 changed (voice rewrites / TTS-readiness fixes), the Stage 4
audio verification result, then whatever `CHAPTER_BUILD_WORKFLOW.md`'s own
Delivery section requires for the video itself.
