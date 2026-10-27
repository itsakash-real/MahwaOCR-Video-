# 03 — Audio System

**What this stage does.** Turns a locked script into narration audio that
is paced evenly enough to cut a sentence-synced video to, and proves —
with whisper timings, not by listening alone — that every paragraph was
actually spoken, in full, in order.

**Locked format file:** `~/workspace/ManhwaRecaps/infinite-titles/AUDIO_FORMAT_LOCKED.md`
(user order 2026-10-27 17:58 IST). Everything in §Locked format below is
that file, quoted in substance. The rest of this document explains why it
is locked that way, and how to execute it.

## Locked format (Infinite Titles long-form, all future audio)

- **Voice:** Deepgram `aura-2-odysseus-en`.
- **Speed:** native **1.3** — the Deepgram native speed parameter, **not**
  ffmpeg `atempo`.
- **Chunking:** **one API POST per paragraph** (blank-line blocks). Not
  per sentence, not the whole script. This is method **B**.
- **Gaps:** **0.25 s** silence between paragraphs.
- **Text cleaning:** line breaks → single spaces only. Do **not** collapse
  `...` to `.`. Do **not** strip emotion punctuation.
- **Script rules still apply on top** (see §Script rules below).
- **FORBIDDEN:** method **C** — native 1.0 + ffmpeg `atempo=1.3`.
- **FALLBACK only:** method **A** — native 1.3, per-sentence, 0.25 s gaps —
  only if paragraph-to-sentence sync recovery proves unreliable on the
  one-chapter test.
- **Existing chapter audio is NOT re-voiced by this lock.** The lock
  governs new / re-voiced chapters only.

## Why B won — the measured test (1,439-word v1 script)

This was decided by measurement, not by preference, on the full Chapter 1
v1 script (1,439 words, 192 sentences, 29 paragraphs). The user delegated
the decision ("you decide") on 2026-10-27 17:34 IST after the Gemini
proposal below was evaluated and not adopted wholesale.

| Method | Total | Pace | Within ±30% of median | Within ±20% |
|---|---|---|---|---|
| **A** — native 1.3, per-sentence | 432.31 s (7:12) | 199.7 WPM | 78.6% | 63.0% |
| **B** — native 1.3, per-paragraph (**winner**) | **408.20 s (6:48)** | **211.5 WPM** | **93.1%** | **82.8%** |
| **C** — 1.0 + `atempo=1.3` (**forbidden**) | 442.92 s (7:23) | 194.9 WPM | 73.4% | 56.2% |

These numbers are measurements **for that script**. Do not quote them as
universal constants for other chapters.

Why A loses: per-sentence POSTs reset pacing at every sentence, so a fast
sentence next to a slow one feels like a gear change.
Why C loses: `atempo` scales every WPM by 1.3 — it cannot reduce relative
spread, and it preserved the uneven pacing while being the slowest overall.
Why B wins, with a caveat: paragraph units naturally smooth the numbers
compared with sentence units, so B's spread advantage is partly a unit
effect — which is exactly why the mandatory checks below exist, and why
B's sentence sync must be **recovered** from whisper word timings rather
than assumed.

**The Gemini proposal that was NOT adopted wholesale** (user-pasted,
2026-10-27 17:33 IST): (1) strip line breaks + collapse `...` → `.`;
(2) chunk by paragraph; (3) generate at 1.0× then `atempo=1.3`.
Evaluation: (1) line-break cleaning is harmless but per-sentence POSTs
already removed line breaks, and collapsing `...` erases user-ordered
emotion punctuation — rejected; (2) paragraph chunking was adopted, but
as native 1.3 (B), not as 1.0+atempo; (3) 1.0+atempo departs from the
locked native 1.3 voice (the user chose 1.3 over 1.0, which was "very
slow") and changes all audio timings — rejected unless tested on one
paragraph sample and explicitly approved, which it was not.

**The truncation caveat (why the completeness check is mandatory).**
A single-post call can **silently truncate**: the first B attempt on a
paragraph test dropped the last 20-word sentence entirely (9.96 s), and
the old B audio for Chapter 1 truncated in two paragraphs — Paragraph 15
(missing "Boss: Goblin Warrior.", "In ten years, this thing has never
come out once.", "Today it did.") and Paragraph 21 (missing
"100 percent." and "And the blow lands."). Only 10 of 29 paragraphs were
fully complete in that audio. That is why Step 3 below is not optional.

## Script rules (voice pacing SOP)

From `VOICE_FIX_ODYSSEUS.md`. Aura-2 is expressive: speed is a global
multiplier that preserves natural prosody, not a flat metronome. Short
punchy sentences read fast; long, emotional, question sentences read
slow; commas / `...` / `!` insert pauses. Aura-2 has **no SSML / pause-tag
control** — you cannot fix pacing with tags. You fix it with the text.
(Pause control in Deepgram docs is Flux batch only, not Aura-2.)

1. Sentence length **10–16 words**, one idea per sentence. Never put a
   3-word shout next to a 25-word description.
2. Commas only where you actually want a pause. One comma per sentence,
   maximum two.
3. Ellipsis `...`: maximum **1 per paragraph**, only for a real dramatic
   pause. Never `... ...`, never `...` at the end of every sentence.
4. Exclamation: maximum **1 per paragraph**. Never `!!!`, never two `!`
   sentences in a row.
5. **No ALL-CAPS** words in spoken text (except a name the model already
   knows). "HE RAN" reads rushed/loud. Write "He ran."
6. **Numbers in words:** "four hundred million", not "400,000,000".
   Digits, decimals, and abbreviations (Mr., Dr.) make the model slow
   down to spell them.
7. No brackets, no `[Beat N]`, no stage directions, no meta text.
8. Questions: keep them short. A long question always slows down.
9. Same structure, same pace inside a paragraph: action paragraphs keep
   all sentences short and similar; emotion paragraphs keep all sentences
   medium. Do not mix lengths wildly inside one paragraph.
10. Voice settings — do not change unless the QA loop fails: keep
    `aura-2-odysseus-en @ 1.3`. Only if outliers persist after a script
    fix, test 1.25 on one chapter. Do not drop to 1.0 (user rejected: too
    slow) and do not go above 1.3.

## Step-by-step procedure

### Step 1 — Prepare the voicing input

1. Start from the locked script (`refined/chapter_N_refined.md`, or a
   user-supplied exact script in `user_scripts_exact/`).
2. Split it into paragraphs on blank lines. Count them. (Chapter 1 v1:
   29 paragraphs. Chapter 1 v2: also paragraph-split; count on disk, do
   not assume 29.)
3. For each paragraph, replace line breaks with single spaces. Do not
   collapse `...`. Do not strip punctuation.
4. Check: label lines = 0, brackets = 0, script rules (§above) applied.
   If a paragraph violates a rule, fix the **text** first (see §QA loop).

### Step 2 — Voice, one POST per paragraph

1. Call Deepgram with model `aura-2-odysseus-en`, speed `1.3` (native
   parameter), one POST per paragraph, in paragraph order.
   Code shape (from `manhwa_ocr/video/tts.py`, as actually coded):
   `deepgram_speak(text, model, api_key, speed)` → POST
   `https://api.deepgram.com/v1/speak?model=&encoding=linear16&sample_rate=24000&container=wav&speed=`,
   header `Authorization: Token <key>`. Key resolution order: `DEEPGRAM_API_KEY`
   env, else `--key-file`, else `tts.key_file` config, else
   `~/.config/manhwa/deepgram.key`. The key is never logged or printed.
   **Do not use the `custom.deepgram` connector** — it is known-blocked
   (401, cannot express the `Token ` prefix). Do not issue reconnect
   cards for it.
2. Stitch the paragraph WAVs in order with **0.25 s** silence between
   paragraphs (paragraph gaps only — not between sentences inside a
   paragraph).
3. Record a manifest: model, speed, chunking method, paragraph count,
   word count, sentence count, total seconds, gaps. Verify the manifest
   against the file with `ffprobe` (duration must match; listening `.m4a`
   copy, if made, must match the `.wav` duration exactly).
4. **Never re-voice a whole chapter for a local fix.** If one paragraph
   is bad, re-voice only that paragraph and re-stitch.

### Step 3 — Mandatory checks before the audio is used for video

All three, every chapter, no exceptions:

1. **Whisper completeness, per paragraph.** Run whisper (small.en,
   `pywhispercpp`, repo venv in the Chapter 1 run) over the stitched
   audio. For every paragraph, the full paragraph text must be present
   in the transcription — no silently dropped sentence. A paragraph
   that fails is re-voiced (text fix first if the paragraph violates a
   script rule) and re-checked. Chapter 1 v2's whole point was fixing
   the two paragraphs that failed this check in the old B audio.
2. **Sentence-sync recovery.** Recover sentence-level timings *inside*
   each paragraph from whisper **word** timings, and verify them against
   the script. Test this recovery on **one chapter first**; method A
   (per-sentence) is the fallback only if this recovery proves
   unreliable on that test. Nominal paragraph windows drift from
   measured speech by up to 0.55 s at edges (Chapter 1 run 1: 43
   sentences outside their nominal window) — **measured whisper times
   are sync truth, nominal windows are not**.
3. **WPM check.** Paragraph WPM within ±30% of the chapter median for at
   least **90%** of paragraphs. Flag outliers and fix by text first,
   re-voice only the outlier; `atempo` 0.9–1.1 on a **single paragraph
   only** as a last resort, never on the whole chapter.

### Step 4 — QA + fix loop (per chapter, before video)

1. Measure WPM per sentence (old pipeline) / per paragraph (locked
   format) from whisper timings.
2. Flag any unit more than 30% faster or slower than the chapter median.
3. For each flagged unit: fix the **text** first (script rules), then
   re-voice **only** that unit.
4. If still off after a text fix: time-stretch only that unit with
   ffmpeg `atempo` 0.9–1.1 (never more, never the whole chapter).
5. Re-check: the flagged unit must now be within 20% of median. If not,
   rewrite the sentence — do not force it.
6. Listen to the chapter start-to-finish once. If a sudden gear change
   remains, it is a script-length problem, not a voice problem — go back
   to the script rules.
7. **Test before a full chapter:** voice one paragraph (5–6 sentences)
   with the rules, listen, check WPM spread. Under 30% → voice the full
   chapter. Over 30% → shorten the long sentences first.

### Step 5 — Freeze the audio

Once the checks pass, the audio is **frozen**. Video never changes audio
to fix a picture problem. Record the frozen duration — video frame
counts are derived from it (see `04-video.md`).

## Scopes that are NOT this format

| Scope | Voice / speed / gaps | Why it differs |
|---|---|---|
| Infinite Titles, original 20-chapter run (ch1–20 audio, incl. ch12–16 fixed) | `aura-2-odysseus-en` @ 1.3, gaps **0.25/0.15** (per-sentence era) | Voiced before the 2026-10-27 paragraph lock; **not re-voiced** by the lock |
| Genius Knight chapters | `aura-2-odysseus-en` @ 1.3, gap **0.11** | Genius chapters already locked that way |
| Solo Swordmaster | `aura-2-odysseus-en` @ **1.35**, gap 0.11 | Separate series lock |
| Mangvoro Shorts | `aura-2-orion-en` @ native 1.3× | Shorts voice identity, not long-form |

Do not "correct" an old chapter's gaps or speed to the new lock. The lock
says explicitly: existing chapter audio is not re-voiced.

## Measured audio reference (Infinite Titles, original run)

All `aura-2-odysseus-en` @ 1.3, ffprobe-verified, under the 480 s cap
unless noted. Use these only as sanity checks for new chapters' pacing,
not as targets to hit exactly: ch1-1600 455.80 s (1,624 words),
ch2 475.13 s, ch3 450.67 s, ch4 428.10 s, ch5 449.23 s, ch6 412.03 s,
ch7 455.47 s, ch8 436.78 s, ch9 450.27 s, ch10 475.70 s, ch11 443.10 s,
ch12 466.46 s (fixed 458.34 s), ch13 458.49 s (fixed 435.81 s),
ch14 452.88 s (fixed 437.32 s), ch15 472.52 s (fixed 453.20 s),
ch16 442.27 s (fixed 433.63 s), ch17 459.25 s, ch18 427.48 s,
ch19 467.69 s (after a 73-word connective trim; first run 493.27 s, not
shipped), ch20 476.53 s.

Chapter 1 v2 (new script, B method): **423.59 s** total. This is the
audio the current Chapter 1 video build uses.

## Failure modes and what to do

| Failure | Do this |
|---|---|
| A paragraph is silently truncated | Re-voice only that paragraph (text fix first if it breaks a script rule), re-stitch, re-run whisper completeness. Never assume a retry is complete — check it. |
| Sentence sync inside a paragraph cannot be recovered | Test on one chapter; if it genuinely fails, fall back to method A (per-sentence) for that chapter and record why. Do not silently use A. |
| WPM outlier persists after text fix + re-voice | `atempo` 0.9–1.1 on that single paragraph only, then re-check within 20% of median. |
| Whole chapter feels like gear changes | Script-length problem. Shorten long sentences, unify paragraph pacing, re-voice affected paragraphs only. |
| Deepgram 401 / auth error | Check key resolution order (§Step 2). Do not use the blocked `custom.deepgram` connector, do not print the key. |

## References

- `~/workspace/ManhwaRecaps/infinite-titles/AUDIO_FORMAT_LOCKED.md`
- `~/workspace/ManhwaRecaps/infinite-titles/VOICE_FIX_ODYSSEUS.md`
- `~/workspace/ManhwaRecaps/infinite-titles/STATUS.md` (per-chapter audio
  durations and manifests)
- `~/workspace/MahwaOCR-Video-/manhwa_ocr/video/tts.py`
