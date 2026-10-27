# Architecture — Transcription System

Companion to `01-transcription.md`. This file describes the design: data
flows, components, invariants, and failure modes. The procedure file tells
you what to do; this file tells you why the system is shaped this way.

## System context

```
External: AsuraScans chapter reader (pages, JSON-embedded image list)
    │
    ▼
[Ingest] ──► pages/chN/ (page images)
    │
    ▼
[Tile] ────► tiles/ (numbered tiles 000… in reading order)
    │
    ▼
[Fan-out: 4 transcription workers, disjoint tile ranges]
    │
    ▼
[Coordinator merge] ──► transcripts/chapter_N.md
    │
    ▼
[Gemini verification, sequential] ──► transcripts/chapter_N_verified.md
    │                                   + verification/chapter_N_verification.md
    ▼
[Recap stage] (see architecture-recap.md)
```

## Data-flow diagram (DFD, level 1)

```
                        AsuraScans
                            │
                    page JSON (first entry =
                    1532×1024 credits banner,
                    rest = 900 px strips)
                            │
                            ▼
              ┌─────────────────────────┐
              │ 1. INGEST               │
              │ Input: chapter id       │
              │ Output: pages/chN/*.jpg │
              │ Store: pages/chN/       │
              └────────────┬────────────┘
                           │ page images
                           ▼
              ┌─────────────────────────┐
              │ 2. TILE                 │
              │ Input: page images      │
              │ Output: tiles 000…      │
              │ Store: tiles/           │
              └────────────┬────────────┘
                           │ tiles (disjoint ranges)
              ┌────────────┼────────────┐
              ▼            ▼            ▼            ▼
         Worker 1     Worker 2     Worker 3     Worker 4
         range A      range B      range C      range D
              │            │            │            │
              └────────────┼────────────┘
                           │ part transcripts
                           ▼
              ┌─────────────────────────┐
              │ 3. MERGE (coordinator)  │
              │ Renumber, dedupe folds, │
              │ reconcile counts        │
              │ Output: chapter_N.md    │
              └────────────┬────────────┘
                           │ merged transcript
                           ▼
              ┌─────────────────────────┐
              │ 4. VERIFY (Gemini,      │
              │    sequential, 4–7      │
              │    chunks)              │
              │ Output: verified md +   │
              │ report                  │
              └─────────────────────────┘
```

Data stores: `pages/chN/`, `tiles/`, `transcripts/` (part, merged,
verified), `verification/`. No database is used; the filesystem is the
store, and filenames are the schema. A file that does not follow the
naming pattern is not part of the pipeline until it is renamed into it.

## Components

| Component | Responsibility | Failure it prevents |
|---|---|---|
| Ingest (`ingest_ch.py`) | Download pages, strict `[a-f0-9]{6}\.webp` extraction, credits banner as entry #1 | JSON-blob-as-URL bug; credits banner counted as a story panel |
| Tile (`tile_ch.py`) | Cut strips into ordered tiles | A worker reading whole pages (context blowout) |
| Transcription worker (×4) | OCR text + situation description for its range only | Single-agent context death (~180 tiles, ~524K tokens) |
| Coordinator merge | Renumber, fold boundaries, reconcile counts, eye-check contested tiles | Double-counted panels, split panels counted twice, numbering drift across ranges |
| Gemini verifier (`gemini_strict.py`, prompt builder) | Sequential verification in 4–7 chunks, OK lines + proposed corrections | Parallel-blast quota ban (429); unverified "corrections" entering the transcript |
| Coordinator reconciliation | Accept/reject each proposed correction on tile evidence; reconcile headers to lines | Gemini header overstatement; invented completions; speaker mis-attribution |

## Key design decisions

1. **Fan-out is a context decision, not a speed decision.** The 4-worker
   split exists because one agent cannot hold ~180 tiles. Splitting
   further (more workers, smaller ranges) is safe; merging workers into
   one is not. The split is computed from the chapter's tile count, not
   copied from another chapter.
2. **Tiles, not pages, are the transcription unit.** Pages are long
   strips; panels span page boundaries and tile boundaries. Only tiles
   give a worker a bounded, ordered input.
3. **The coordinator is the only merger.** Workers never see each
   other's ranges, so only the coordinator can resolve a panel that
   spans a range boundary. This is why part files use part-internal
   numbering and merged numbering is assigned at merge time.
4. **Verification is a check, not an edit.** Gemini proposes; the
   coordinator disposes, on tile evidence. A verifier that edited the
   transcript directly would inject invented text (observed: invented
   date completions, invented window text, speaker re-attribution).
5. **Sequential Gemini is a quota decision.** Quota is per project, not
   per key, so parallel calls do not multiply capacity — they multiply
   429 risk. One key at a time, rotate only on 429, backoff, circuit
   breaker, stop on unrecovered 429.
6. **Flags are data, not defects.** "Speaker unclear", "text cut off",
   "illegible" are preserved through verification into the recap, where
   they become neutral paraphrase. Resolving a flag by guessing creates
   a false fact that every downstream stage then treats as true.

## Invariants

- Tile sections in the merged transcript are contiguous, no gaps.
- Story panels + non-story items = reconciled OK-line count.
- Verified transcript body = merged transcript body + coordinator-
  accepted corrections only.
- Every story panel has: panel id, all visible text exactly as printed,
  a situation description, and any applicable flags.
- No key value, no credential, appears in any output file.

## Failure modes (design view)

| Failure | Design response |
|---|---|
| Worker context death | Smaller ranges, re-run only that range, re-merge. The architecture assumes workers are disposable; the coordinator is not. |
| Range-boundary panel counted twice | Merge dedupes folds; coordinator eye-checks contested boundaries on tiles. |
| Gemini 429 / ban | Sequential + backoff + rotate-only-on-429 + stop. Never "catch up" in parallel. |
| Gemini invents a completion | Coordinator rejects on tile evidence; rejection is recorded, not silently dropped. |
| A later stage "fixes" a transcript fact from memory | Forbidden. A transcript change requires new panel evidence and a new verification pass, not a memory edit. |

## References

- `01-transcription.md` (procedure)
- `~/workspace/ManhwaRecaps/infinite-titles/STATUS.md` (per-chapter
  evidence for every design decision above)
