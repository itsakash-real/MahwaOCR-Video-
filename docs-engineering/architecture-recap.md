# Architecture — Recap System

Companion to `02-recap.md`. Design, data flows, and failure modes for
turning a verified transcript into a locked narration script.

## System context

```
Verified transcript (transcripts/chapter_N_verified.md)
    │
    ▼
[Recap writer] ──► recaps/chapter_N_recap.md (beats, story order)
    │                (+ _clean.md if labels were stripped)
    ▼
[Pre-TTS checks] ──► labels = 0, brackets = 0, pacing rules, word band
    │
    ▼
[Gemini refinement, sequential, 2 chunks] ──► refined/chapter_N_refined.md
    │                                          (+ _fixed.md / _pretrim.md as needed)
    ▼
[Audio stage] (see architecture-audio.md)
    │
    └─ (later) ─► [Chapter 1 rule] fix only the affected paragraph, re-voice only it
```

## Data-flow diagram (DFD, level 1)

```
   ┌──────────────────────┐
   │ Verified transcript  │  (full text, never a digest)
   │ + series bible       │  (names, facts as of this chapter)
   │ + panel list         │
   └──────────┬───────────┘
              │
              ▼
   ┌──────────────────────┐
   │ 1. RECAP WRITER      │  Beats in story order
   │    - cold open       │  Panel citations in draft
   │    - plot in order   │  (removed in final)
   │    - end mid-        │  [UNCLEAR] → neutral
   │      escalation      │  paraphrase, never named
   └──────────┬───────────┘
              │ recap draft
              ▼
   ┌──────────────────────┐
   │ 2. PRE-TTS CHECKS    │  Labels, brackets, visual
   │    (coordinator)     │  words, word band, pacing
   └──────────┬───────────┘
              │ checked recap
              ▼
   ┌──────────────────────┐
   │ 3. GEMINI REFINEMENT │  2 chunks, sequential
   │    (polish only)     │  Key 1, rotate only on 429
   └──────────┬───────────┘
              │ refined draft
              ▼
   ┌──────────────────────┐
   │ 4. DIFF + REVERT     │  Word-by-word diff vs
   │    (coordinator)     │  original; revert any
   │                      │  meaning change
   └──────────┬───────────┘
              │ locked script
              ▼
   ┌──────────────────────┐
   │ 5. CAP CHECK         │  Over 480 s at measured
   │    (if needed)       │  pace → connective trim
   │                      │  only, preserve pre-trim
   └──────────────────────┘
```

Data stores: `transcripts/`, `recaps/`, `refined/` (including `_fixed`
and `_pretrim` files, which are never overwritten by later fixes).
The `_fixed` / `_pretrim` pattern is the versioning system: the original
is immutable, every fix is a new file alongside it.

## Components

| Component | Responsibility | Failure it prevents |
|---|---|---|
| Recap writer | Story-order beats from the full transcript, transcript-only facts | Invented facts; dropped beats; out-of-order plot |
| Pre-TTS checks | Label/bracket/visual-word counts, word band, pacing rules | Beat-label lines being read aloud (the ch12–16 defect); TTS pauses from brackets/meta |
| Gemini refinement | Light polish, 2 sequential chunks | Parallel-blast ban; meaning drift if unchecked |
| Diff + revert (coordinator) | Word-by-word comparison, revert failure modes | Quote-stripping, parenthesised titles, sentence-per-line, speaker re-attribution, digitised numbers |
| Cap check | Connective-tissue trim only, pre-trim preserved | Beat/fact loss to hit a duration cap; silent script replacement |

## Key design decisions

1. **The transcript is the only fact source.** The recap writer does not
   get a digest, a wiki summary, or prior chapters' recaps as fact
   inputs. Prior chapters supply "previously" context and series-bible
   continuity (names spelled one way), not facts about this chapter.
2. **Refinement is polish, not authorship.** Gemini's job is light
   polish. The coordinator's diff is the actual gate: any refinement
   that changes meaning is reverted, even if it reads better. This is
   why refinement is 2 chunks (small enough to diff reliably) and why
   whole-chunk reverts are normal, not exceptional.
3. **Immutability + sidecar fixes.** Originals are never edited.
   `_fixed` (label removal) and `_pretrim` (cap trim) files sit
   alongside the original, so any stage can always return to the
   pre-fix script and any auditor can see exactly what changed.
4. **The Chapter 1 rule is a versioning rule.** "Fix only that
   paragraph" works because scripts are paragraph-addressed and audio is
   paragraph-chunked (see `architecture-audio.md`). A script fix that
   cannot be addressed to a paragraph is not a local fix — it is a
   rewrite, and it needs user approval.
5. **Word band is a pacing proxy, not a quality target.** The 1,550–
   1,650 band exists because, at measured paces, it lands under the
   480 s audio cap. When pace is slow (ch10: ~0.306 s/word), the band
   top is unsafe and the target moves to ~1,550–1,600. The cap (480 s)
   is the rule; the band is the heuristic.
6. **User-supplied exact scripts bypass this pipeline by design.**
   Chapters 1–4 (2026-10-27) have user scripts that are used exactly as
   supplied. The recap/refinement pipeline does not "improve" them, and
   the word band does not apply to them. This is an input-supersession
   rule: the user's file, when supplied as exact, outranks the pipeline.

## Invariants

- Every factual claim in a script is citable to a panel in the verified
  transcript (citations removed in the final, but the citability
  remains).
- `[UNCLEAR]` speakers are never named; unnamed figures are never
  merged; the mystery knight and the silver-haired woman stay separate.
- Final script: 0 label lines, 0 brackets in the body, 0 banned visual
  words as narration devices, ellipses only inside quoted dialogue.
- Audio duration ≤ 480 s per chapter (original run); later user scripts
  are governed by their own delivery, not retroactively by this cap.
- No script file is ever overwritten by a fix; fixes are new files.

## Failure modes (design view)

| Failure | Design response |
|---|---|
| Gemini strips all quotes in a chunk | Revert the whole chunk. Do not repair Gemini's version sentence by sentence — the failure is systematic, not local. |
| Gemini parenthesises title sentences | Restore original title sentences (recurring failure mode, ch9–ch14). |
| Beat labels reach TTS | Pre-TTS label count = 0 gate; `_clean`/`_fixed` sidecar; original preserved. This defect shipped once — the gate exists because of it. |
| Script over cap | Connective trim only; quote count unchanged; pre-trim preserved. |
| A fact is challenged downstream | Trace it to a panel citation. If it cannot be traced, delete it — do not defend it from memory. |

## References

- `02-recap.md` (procedure)
- `~/workspace/ManhwaRecaps/infinite-titles/STATUS.md`
- `~/workspace/MahwaOCR-Video-/docs/RECAP_STYLE_MANHWABRO.md`
