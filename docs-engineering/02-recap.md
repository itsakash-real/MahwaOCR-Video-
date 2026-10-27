# 02 — Recap / Script System

**What this stage does.** Turns a verified transcript into a narration
script that a voice actor (in this pipeline, Deepgram) can read aloud as a
continuous movie-style recap: a cold open, then the plot in story order,
ending mid-escalation so the next chapter opens mid-flow. The script is the
contract for audio and for video mapping — every sentence in it will get
exactly one shot.

**Session home (Infinite Titles):** `~/workspace/ManhwaRecaps/infinite-titles/`
(`recaps/`, `refined/`, `user_scripts_exact/`)

## Inputs and outputs

| | |
|---|---|
| Input | `transcripts/chapter_N_verified.md` (full transcript, never a digest) |
| Intermediate | `recaps/chapter_N_recap.md` (recap draft, beats) |
| Intermediate | `recaps/chapter_N_recap_clean.md` (only if labels were stripped — see below) |
| Output | `refined/chapter_N_refined.md` (Gemini-refined script, the voicing input) |
| Output (pre-trim, when a trim was needed) | `refined/chapter_N_refined_pretrim.md` (original preserved, never overwritten) |
| Output (fixed, when a label fix was needed) | `refined/chapter_N_refined_fixed.md` (original preserved, never overwritten) |
| Special input (2026-10-27) | `user_scripts_exact/chapter_0N_script_exact*.txt` — user-supplied exact scripts, which **supersede** the recap/refined pipeline for chapters 1–4 |

## The style: manhwabro

The pinned recap style for this lane is `manhwabro`
(spec: `~/workspace/MahwaOCR-Video-/docs/RECAP_STYLE_MANHWABRO.md`,
registered in config as `recap.style: manhwabro`, lint
`script_lint.lint_manhwabro`). Its rules, as specified:

- **Source of truth: the whole chapter transcript** — OCR dialogue and
  narration plus the written panel descriptions. The writer gets the
  complete transcript, the series bible as of that chapter, and the panel
  list. Names come from the transcript and series context, spelled one way
  throughout. Appearance, emotion, and action come from the descriptions.
- **Structure:** (1) cold open in the chapter's strongest fight or
  confrontation (range opener only); (2) pivot question ("So, why is
  [protagonist] so strong?"); (3) lore / flashback, past tense only
  inside the flashback; (4) present-day setup and the protagonist's
  resolve; (5) **end mid-escalation** — later chapters open mid-flow on
  their best beat. Cold open is capped at **30–45 seconds** of narration.
- **Voice:** sarcastic, bro-ish fan — storyteller second, hype man first.
  Epithets for the protagonist ("our boy", "this brat", "bro", "this man");
  insults for others ("clown", "old fart", "the poor bastard"); mock
  sympathy; internet slang in fantasy lore; at most one pop-culture
  comparison per chapter; short punchy sentences with an occasional long
  run-on landed by a short one; pivots (But / Then / And before X can
  recover / Well, of course); ironic understatement, deadpan stacking,
  hyperbole about the protagonist's power.
- **Jokes and insults are commentary. They never add or change plot.**
- **Lint enforces (pattern checks, not taste):** `finale-ending`,
  `restart-opening`, `camera-cue`, `camera-direction`, `cue-form`,
  plus manhwabro-specific `bro-pivot` and `past-life-opening` (range
  opener only) and `bro-epithet` (every chapter).

**Scope note.** The manhwabro spec describes the Solo / lane style. The
Infinite Titles user-supplied scripts for chapters 1–4 (see §User-supplied
exact scripts) are the governing scripts for those chapters regardless of
tone — they are used **exactly as supplied**, and this style file does not
override them.

## Step 1 — Write the recap (from the verified transcript only)

1. Read the **full** verified transcript. No digest, no previous chapter's
   recap as a substitute for the transcript.
2. Write the recap in **beats** (numbered story beats in story order),
   covering all important panels. A recap that drops a beat or a fact to
   hit a word count is wrong — trim connective tissue first, never a beat
   or a fact (see Step 4).
3. **Fact rule (hard):** every factual claim in the recap must be in the
   transcript. If it cannot be cited to a panel, delete it. This is the
   same rule given for outside script help: cite the panel number
   (`pXX-YY`) after each beat in a draft, then remove the citations in
   the final script.
4. **Do-not-name / do-not-merge rules (from the verified transcripts):**
   - `[UNCLEAR]` speakers are paraphrased **neutrally, never named**.
   - Unnamed figures stay unnamed: the hospital woman (ch15) is never
     named; the office woman (ch16) is not merged with ch9/ch15 figures;
     the blond debt-collector is never assumed to be Arai; goateed man /
     bob-haired woman are Toshiue's people, unnamed; the ch10 furious man
     is not asserted to be the chief examiner.
   - The mystery knight (`?????? Lv.8`, ch7–8) and the silver-haired woman
     are kept **separate** — never confirm they are the same person.
   - Other characters only *suspect* a stackable-titles item — that is
     their theory, not a confirmed fact (ch5, ch14).
5. **Banned in the spoken script:** `[Beat N]` labels, brackets, stage
   directions, meta text, and visual words used as narration
   (`panel`, `frame`, `camera`, `caption`, `off-screen`, `view`, `bubble`
   as a narration device). These get read aloud or cause odd pauses.
   **This is a defect that actually shipped once:** chapters 12–16
   carried beat-label lines into TTS because the TTS builder did not
   strip them, and had to be re-voiced from `_fixed` scripts. The catch
   is now a pre-TTS check: count label lines in the voicing input; it
   must be 0. Chapter 17's recap was caught at this check and stripped
   into `recaps/chapter_17_recap_clean.md` before refinement.
6. **Word bands — keep the scopes separate:**
   - Infinite Titles original run: target **~1,600 words**, acceptable
     **1,550–1,650**, with a hard audio cap of **8:00 (480 s)** per
     chapter. At measured paces this band is tight: ch2 landed 475.12 s,
     ch10 475.70 s, ch20 476.53 s — all under the cap by under 5 s.
     Lesson recorded in STATUS: at the slowest measured pace
     (~0.306 s/word including gaps), the band top is unsafe; target
     **~1,550–1,600 words** for margin, never the band top.
   - Solo long-form: **560–920 words**, 3–5 minutes.
   - User-supplied exact scripts (ch1–4, 2026-10-27): **1,439 / 1,457 /
     1,404 / 1,349 / 1,379 words** — these are below the 1,550–1,650 band
     and are used exactly as supplied anyway. Do not "correct" them up
     to the band.
7. **No intro/outro, no finale language at chapter ends** (join-safe for
   the continuous movie), no meta, no `[Beat N]` labels in the final.

## Step 2 — Gemini refinement (sequential, ban-safe)

1. Send the recap to Gemini in **2 chunks, sequentially** (the pattern
   used for ch11–20; earlier chapters used 3–4 chunks — chunk count is
   not the rule, sequential is).
2. Gemini returns a lightly polished script. The coordinator then
   **diffs it word by word** against the original and **reverts** any
   change that alters meaning or breaks a house rule. Recurring Gemini
   failure modes, all observed repeatedly and all reverted on sight:
   - Stripping **all dialogue quotes** from a chunk (ch9 quote-to-
     narration, ch15 chunk 2, ch17 beats 8–10/12–13, ch19 chunk 1,
     ch20 chunk 2) — revert the whole chunk to originals.
   - Parenthesised title/effect sentences (ch9, ch11, ch12, ch13, ch14) —
     restore the original title sentences.
   - Sentence-per-line output (ch9, ch14, ch16) — rejoin into beat
     paragraphs if the sentences are verbatim, otherwise revert.
   - Digitised numbers, garbled endings, dropped terms, speaker
     re-attribution, emphasis-caps flattening, "camera"/"frame"/"bubble"
     wording, nonsense sentences — revert the affected beat.
   - **Rule:** a refinement that changes meaning is not a refinement.
     When in doubt, the original beat wins.
3. Check the refined file: 0 em-dash in the body (title-header dash only),
   0 brackets, 0 parentheses in the body, 0 label lines, 0 banned visual
   words. Ellipses are allowed **only inside quoted panel dialogue**.
4. Write `refined/chapter_N_refined.md`. Never overwrite it with a later
   fix — write `_fixed` or `_pretrim` alongside it.

## Step 3 — Pre-TTS checks

1. Word count and sentence count recorded.
2. Label-line count = 0. Bracket count = 0 (body).
3. Voice pacing rules applied (see `03-audio.md` §Script rules): sentences
   10–16 words, one idea each, `...` max 1/paragraph, `!` max 1/paragraph,
   no ALL-CAPS, numbers in words, no brackets/meta.
4. If the script is over the audio cap at measured pace, trim **connective
   tissue only** (73 words in ch19, 63 words in ch10, both with quote
   counts unchanged and no beat/fact dropped), preserve the pre-trim
   script as `refined/chapter_N_refined_pretrim.md`, and re-check.

## Step 4 — If a script issue is found later (the Chapter 1 rule)

If audio or video work exposes a script problem (truncation, a garbled
paragraph, a wrong number), **fix only that part**:

- Chapter 1 v1 → v2 is the worked example. The v2 diff, verified on disk,
  is exactly: Paragraph 15 colons → commas ("Dungeon, Goblin Den. Boss,
  Goblin Warrior." / "Today, it did."); Paragraph 21 numbers in words
  ("Level one. One hundred percent."); and the "Slash. Blood. A scream.
  White. Silence." beat replaced by one full sentence ("The heavy blade
  slashes down, drawing a spray of blood and a sharp scream before the
  world fades into a blinding white silence."). Those were exactly the
  two paragraphs that had truncated in the old B audio.
- Re-voice **only** the fixed paragraph(s). The rest of the chapter's
  audio is untouched. Never rewrite or re-voice the whole script for a
  local fix.

## User-supplied exact scripts (chapters 1–4, 2026-10-27)

These supersede the recap/refined pipeline for chapters 1–4. Verified on
disk 2026-10-27 (word counts by `wc -w`, MD5s by `md5sum`):

| File | Words | MD5 |
|---|---|---|
| `chapter_01_script_exact.txt` (v1) | 1,439 | `dc5b0c551e721a2c5211c2d193e42d28` |
| `chapter_01_script_exact_v2.txt` (v2, current) | 1,457 | `bdf9e3aab32f3ee1535f337d6735f1aa` |
| `chapter_02_script_exact.txt` | 1,404 | `ad3957d84bb551fe207d68b3234f48aa` |
| `chapter_03_script_exact.txt` | 1,349 | `23966d5d40d90000bda6735865569ab8` |
| `chapter_04_script_exact.txt` | 1,379 | `89b145e6c5b2e96ba97f2c4d5e4b5315` |

Location: `~/workspace/ManhwaRecaps/infinite-titles/user_scripts_exact/`.
Originals also in `~/workspace/user/files/`. If a future MD5 does not match
this table, the file on disk has changed — re-verify with the user, do not
assume this table is wrong or the file is right.

## Failure modes and what to do

| Failure | Do this |
|---|---|
| Recap invents a fact | Delete it. If it cannot be cited to a panel, it does not stay, however good it sounds. |
| Gemini refinement strips quotes / changes meaning | Revert the affected chunk/beat to the original. Do not "fix" Gemini's version. |
| Beat-label lines reach TTS input | Stop. Strip them into a `_clean`/`_fixed` file, preserve the original, re-check label count = 0 before voicing. |
| Script over the 480 s cap | Trim connective tissue only, preserve pre-trim, re-check quote count and beats. |
| A later stage finds a script error | Fix only that paragraph, re-voice only that paragraph (Chapter 1 rule). |

## References

- `~/workspace/ManhwaRecaps/infinite-titles/STATUS.md` — recap/refinement
  outcomes, failure modes, and word counts for ch1–20.
- `~/workspace/ManhwaRecaps/infinite-titles/recaps/`,
  `refined/` (including `_fixed` and `_pretrim` files).
- `~/workspace/ManhwaRecaps/infinite-titles/user_scripts_exact/`.
- `~/workspace/MahwaOCR-Video-/docs/RECAP_STYLE_MANHWABRO.md`.
- `~/workspace/ManhwaRecaps/infinite-titles/VOICE_FIX_ODYSSEUS.md`
  (script pacing rules, also in `03-audio.md`).
