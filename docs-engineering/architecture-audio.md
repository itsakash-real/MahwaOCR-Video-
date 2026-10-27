# Architecture — Audio System

Companion to `03-audio.md`. Design, data flows, and failure modes for
turning a locked script into frozen, sentence-synced narration audio.

## System context

```
Locked script (refined/*.md or user_scripts_exact/*.txt)
    │
    ▼
[Paragraph splitter] ──► paragraphs (blank-line blocks)
    │
    ▼
[Deepgram TTS, one POST per paragraph, native 1.3] ──► paragraph WAVs
    │
    ▼
[Stitcher: 0.25 s gaps] ──► audio/chapter_N/audio.wav + manifest
    │
    ▼
[Whisper checker] ──► completeness per paragraph + word timings
    │
    ▼
[Sync recovery] ──► sentence timings inside paragraphs
    │
    ▼
[WPM checker + QA loop] ──► frozen audio (video input)
```

## Data-flow diagram (DFD, level 1)

```
   ┌──────────────────────┐
   │ Locked script        │
   └──────────┬───────────┘
              │ split on blank lines
              ▼
   ┌──────────────────────┐
   │ 1. PARAGRAPH SPLIT   │  Line breaks → single
   │    + CLEAN           │  spaces; ... preserved
   └──────────┬───────────┘
              │ paragraphs P1…Pn (n = 29 for ch1 v1)
              ▼
   ┌──────────────────────┐
   │ 2. DEEPGRAM TTS      │  POST /v1/speak per paragraph
   │    aura-2-odysseus   │  model, linear16, 24 kHz,
   │    -en @ native 1.3  │  wav, speed=1.3
   │    Header: Token     │  Key: env → --key-file →
   │    <key> (never      │  config → ~/.config/manhwa/
   │    logged)           │  deepgram.key
   └──────────┬───────────┘
              │ paragraph WAVs, in order
              ▼
   ┌──────────────────────┐
   │ 3. STITCH            │  0.25 s silence between
   │                      │  paragraphs only
   └──────────┬───────────┘
              │ stitched WAV + manifest
              ▼
   ┌──────────────────────┐
   │ 4. WHISPER CHECK     │  small.en, pywhispercpp
   │    (mandatory)       │  Completeness per paragraph;
   │                      │  word timings out
   └──────────┬───────────┘
              │ word timings
              ▼
   ┌──────────────────────┐
   │ 5. SYNC RECOVERY     │  Sentence boundaries from
   │    (mandatory)       │  word timings, verified
   │                      │  against script
   └──────────┬───────────┘
              │ sentence timings (sync truth)
              ▼
   ┌──────────────────────┐
   │ 6. WPM CHECK + QA    │  ≥90% of paragraphs within
   │    LOOP              │  ±30% of median; text fix
   │                      │  → re-voice paragraph only
   │                      │  → atempo 0.9–1.1 single
   │                      │  paragraph, last resort
   └──────────┬───────────┘
              │ FROZEN AUDIO
              ▼
        [Video stage]
```

Data stores: paragraph WAVs (intermediate), stitched `audio.wav`,
`tts_manifest.json`, listening `.m4a` copy (duration must match the
`.wav` exactly), whisper outputs (`whisper_words.json`,
`whisper_segments.json`, `sentence_timings_*.json` in the chapter
workdir).

## Components

| Component | Responsibility | Failure it prevents |
|---|---|---|
| Paragraph splitter | Blank-line split, line-break cleaning only | `...` collapse erasing emotion punctuation; whole-script POST truncation |
| Deepgram caller (`manhwa_ocr/video/tts.py`: `deepgram_speak`, `resolve_voice_and_speed`) | Model/speed validation (Aura-2 only, 0.7–1.5), key resolution, POST shape | Wrong model/speed silently accepted; key printed in logs; non-Aura speed sent |
| Stitcher | Ordered concat + 0.25 s paragraph gaps | Sentence-gap drift; paragraph order errors |
| Whisper checker | Per-paragraph completeness, word timings | Silent truncation shipping into video (the ch1 Paragraph 15/21 defect) |
| Sync recovery | Sentence timings from word timings | Nominal-window drift (up to 0.55 s at edges) becoming sync error |
| WPM checker + QA loop | Median-spread flags, text-first fixes, paragraph-only re-voice | Gear-change pacing; whole-chapter re-voice for a local outlier |

## Key design decisions

1. **Paragraph is the unit of voicing, of checking, and of fixing.**
   This is the central design choice. Per-sentence voicing (A) resets
   pacing every sentence and multiplies API calls; whole-script voicing
   risks silent truncation of a long call and gives no local fix unit.
   Paragraph voicing gives natural prosody inside a paragraph, a
   bounded call that can still truncate (hence the mandatory check),
   and a fix unit that is small enough to re-voice cheaply.
2. **Native speed, not post-speed.** Deepgram's native 1.3 is a prosody-
   preserving multiplier applied during synthesis. FFmpeg `atempo`
   after the fact scales everything uniformly, including pauses, and
   cannot reduce relative spread — it makes a slow paragraph and a fast
   paragraph both 1.3× as uneven. That is why C is forbidden, and why
   `atempo` is allowed only as a last resort on a single paragraph.
3. **Measured timings are sync truth.** Nominal paragraph windows are
   estimates from word counts and gaps. Whisper word timings are
   measurements of what was actually spoken. Video frame counts are
   derived from measured timings, never from nominal windows. The
   Chapter 1 run 1 data proves the gap: 43 sentences outside nominal
   windows, up to 0.55 s drift at edges.
4. **Checks are gates, not reports.** Completeness, sync recovery, and
   WPM are mandatory before video use. A chapter that fails any of
   them does not proceed to mapping — it goes back through the QA loop.
   The old B audio shipped (in an earlier run) with only 10/29
   paragraphs fully complete; the lock exists so that cannot recur.
5. **Text-first fixing.** Pacing is a text property in Aura-2 (no SSML,
   no pause tags). The QA loop therefore fixes text before it touches
   audio, and touches audio (atempo) only as a last resort, on one
   paragraph, within 0.9–1.1. A system that fixed pacing by re-voicing
   the same text repeatedly would never converge.
6. **Key handling is a separate, narrow path.** Key resolution is
   env → `--key-file` → config → default key file, and the key is
   never logged. The blocked `custom.deepgram` connector is not a
   fallback — it cannot express the `Token ` prefix and returns 401.
   Do not route around it with reconnect cards.

## Invariants

- Stitched audio = paragraphs in script order, 0.25 s gaps between
  paragraphs, no gaps added inside paragraphs.
- Every paragraph in the script appears in full in the whisper
  transcription of the audio.
- Sentence timings used by video come from whisper word timings,
  verified against the script — never from nominal windows.
- ≥90% of paragraphs within ±30% of the chapter median WPM.
- Frozen audio is immutable for video purposes. A video picture problem
  never changes audio; an audio problem follows the QA loop and then
  the chapter re-gates.
- Manifest duration = ffprobe duration of the `.wav` = duration of the
  listening `.m4a` (if made).

## Failure modes (design view)

| Failure | Design response |
|---|---|
| Silent paragraph truncation | Whisper completeness per paragraph; re-voice only that paragraph; re-check the retry (a retry is not assumed complete). |
| Sync recovery unreliable | Test on one chapter; fall back to method A for that chapter, recorded, not silent. |
| WPM outlier | Text fix → re-voice paragraph → atempo 0.9–1.1 single paragraph → rewrite sentence. Never whole-chapter atempo. |
| Deepgram 401 | Key-resolution order; never the blocked connector; never print the key. |
| Listening copy duration ≠ WAV duration | Treat as a build error; rebuild the copy, do not ship the mismatch. |

## References

- `03-audio.md` (procedure)
- `~/workspace/ManhwaRecaps/infinite-titles/AUDIO_FORMAT_LOCKED.md`
- `~/workspace/ManhwaRecaps/infinite-titles/VOICE_FIX_ODYSSEUS.md`
- `~/workspace/MahwaOCR-Video-/manhwa_ocr/video/tts.py`
