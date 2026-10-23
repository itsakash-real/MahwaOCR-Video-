# Recap Style: Manhwa Bro

A narration style reverse-engineered from a manhwa-recap channel sample
(a regression-archmage recap). The narrator is a sarcastic, bro-ish fan:
a storyteller second, a hype man first.

Enable per series in `manhwa-ocr.yaml`:

```yaml
recap:
  style: manhwabro   # default: default (the calm present-tense voice)
```

Pick one voice per series; do not mix lanes inside one video.

## Source of truth: the full transcript

This style is written from the WHOLE chapter transcript: the OCR dialogue and
narration, plus the written descriptions of each character and each panel's
situation. The prompt hands the writer the complete transcript (not a digest),
the series bible as of that chapter, and the panel list. Rules:

- Names come from the transcript and series context, spelled one way throughout.
- Appearance, emotion and action come from the descriptions.
- Jokes and insults are commentary. They never add or change plot.

## Structure

1. **Cold open** in the chapter's strongest fight or confrontation (range opener only).
2. **Pivot question**: "So, why is [protagonist] so strong?"
3. **Lore / flashback**, past tense only inside the flashback.
4. **Present-day setup** and the protagonist's resolve.
5. **End mid-escalation.** Later chapters open mid-flow on their best beat.

## Voice

- Epithets for the protagonist: our boy, this brat, bro, this man, our cool epic protagonist.
- Insults for others: clown, old fart, the poor bastard; mock sympathy ("poor guy").
- Internet slang in fantasy lore (mogged, Chad, incel, "has the balls to", "ain't no").
- At most one pop-culture comparison per chapter.
- Short punchy sentences, an occasional long run-on, then a short one to land the joke.
- Pivots: But / Then / And before X can recover / Well, of course.
- Ironic understatement, deadpan stacking, hyperbole about the protagonist's power.

## What the lint enforces (`script_lint.lint_manhwabro`)

Shared with Power Fantasy: `finale-ending`, `restart-opening`, `camera-cue`,
`camera-direction`, `cue-form`. Added here: `bro-pivot` and `past-life-opening`
(range opener only), and `bro-epithet` (every chapter). These are pattern
checks, not taste judgments: tone and humor are the prompt's job.

## Files

`MANHWABRO_SYSTEM`, `MANHWABRO_PROMPT` in `manhwa_ocr/script.py`;
`lint_manhwabro` in `manhwa_ocr/script_lint.py`; registered in
`manhwa_ocr/config.py` (`recap.style`).
