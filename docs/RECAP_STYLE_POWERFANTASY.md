# Recap Style: Power Fantasy

The high-retention "power fantasy" recap system: an in-medias-res hook with
an effortless flex inside the first 30 seconds, the lore dropped only after
the flex, a hype-man narration voice ("our guy", enemies are "the poor guy"),
and gamified vocabulary. The system's text is the user's own, stored verbatim
as `POWERFANTASY_SYSTEM` in `manhwa_ocr/script.py` — that constant is the
versioned template; this doc describes how the pipeline runs it.

Enable per series in `manhwa-ocr.yaml`:

```yaml
recap:
  style: powerfantasy   # default: default (the calm present-tense voice)
```

This is a *different lane* from the default voice and from Speedrun Gossip
(docs/RECAP_STYLE_SPEEDRUN.md). Do not mix voices in one series.

---

## 1. The system (summary — the verbatim text lives in script.py)

1. **THE HOOK (first 60 seconds).** Never start with the MC's past,
   reincarnation or childhood. Start in medias res: the MC in their current
   low-status disguise facing an arrogant enemy, and humiliate/defeat that
   enemy effortlessly within the first 30 seconds. Opening formula:
   "This guy is [Name], a [low status role] at [prestigious place], but..."
2. **THE LORE DROP (~minute 2).** Only after the flex: "But what none of
   them know is..." — then the past life / reincarnation / system awakening,
   compressed with rapid-fire time skips ("By week 1...", "At age 15...").
3. **TONE & LEXICON.** Informal, fast, conversational. The MC is "our guy",
   "our boy", "our protagonist"; enemies about to lose are "the poor guy" /
   "this absolute amateur". Action adverbs: "casually destroys",
   "effortlessly dodges", "brutally shuts down". No filler, no long
   exposition; sentences flow on conjunctions.
4. **THE HYPE MAN RULE.** Power is proven through other people's shock,
   terror and awe — dedicate heavy focus to bystander/enemy reactions.
5. **GAMIFIED VOCABULARY.** "OP", "status window", "aggro", "tutorial
   boss", "plot armor", "line of sight", "stats".

Output is a spoken voiceover script only — no camera directions, no visual
notes, only the exact words the narrator speaks.

## 2. The range workflow (coded, not a convention)

For a target range — say chapters 1–10 — do **not** recap chapter by chapter
as transcripts trickle in. First finish the transcripts of **all** chapters
in the range; only then write the recaps, with the whole story in hand.

`RecapBuilder` enforces this for `recap.style: powerfantasy`: `prepare` and
`generate` refuse with a `UserError` listing every missing chapter until the
transcripts for the full contiguous range spanned by the request exist
(requesting `2` and `4` requires 2, 3 and 4). Other styles are unaffected.

## 3. Prompt assembly (per chapter N of the range)

The chapter prompt (`POWERFANTASY_PROMPT` in `manhwa_ocr/script.py`) carries:

- **The series bible for the whole range** — `SeriesMemory.as_briefing()`
  with no `upto` cut-off. (The default style deliberately briefs only "as of
  this chapter"; this style deliberately knows the whole arc.)
- **A RANGE CONTEXT block** — a beat digest of every *other* chapter in the
  range, compressed from its transcript (each scene heading + the first line
  of its body; 700 chars per chapter, 6000 chars total, oldest digest dropped
  first). Chapter N's own **full transcript** is included separately, as in
  every style. Full transcripts of the whole range are *not* embedded — they
  would drown the chapter being written; the bible + digests are the
  whole-story context.
- **The previous chapter's recap ending** and story-so-far summaries, when
  they exist (the standard continuity block). On a fresh range prepared in
  one pass, earlier recaps don't exist yet at prepare time — the range block
  is what carries the continuity there; in provider mode (sequential) and on
  re-prepares the previous ending is included too.
- **The panel block**, unchanged: tag every spoken sentence with its panel.
- **A scope note + framing section** (the pipeline adaptations, below).

## 4. Framing adaptations (pipeline, kept separate from the system text)

- **SCOPE.** The full Hook + Lore Drop structure applies to the **first
  chapter of a range only**. Later chapters open mid-flow on a strong beat
  (a mini-hook): no lore re-drop, no "This guy is..." re-introduction, no
  restart language.
- **CONTINUITY (binding user rule).** The chapter videos are joined into one
  continuous film, so a chapter must never end with finale/closing language
  and never open with restart/"welcome back" language. Endings land
  mid-escalation, so the join is invisible in the script.
- **TAG LAYER.** The recap file keeps the repo's panel-tag markup on
  sentences exactly as in the other styles (pairing/alignment run on the
  tags); the narration text itself stays pure voiceover.
- **TITLE-CARD CUES.** A genuine time skip is written as its own standalone
  sentence in a form the title-card generator detects ("Age fifteen.",
  "Week six of possession.", "Year four of possession.", "Three years
  later."), so the video's split-flap card fires on the spoken cue.

## 5. The script lint gate (`manhwa_ocr/script_lint.py`)

`lint_powerfantasy` in `manhwa_ocr/script_lint.py` (registered in
`manhwa_ocr/config.py`, `recap.style`): pure regex checks, run in the finalize path
(and provider-mode generate) for this style only. A failing chapter is **not
saved** — fix the narration and re-finalize, exactly like a tag error.

Range opener only (`range_opener=True`: first chapter of the task's range
with no earlier recap on disk):

- **hook-formula** — the opening (first 8 sentences) must match the formula
  shape `This guy is ..., a ... at ..., but...`.
- **lore-drop** — the phrase "but what none of them know is" must appear.
- **past-life-opening** — none of these markers in the first 3 sentences:
  `reincarnat`, `past life`, `previous life`, `former life`, `was reborn`,
  `transmigrat`, `soul entered`, `before he died`, `after his death`,
  `his childhood`, `her childhood`, `as a child`, `when he was young`,
  `when she was young`. (A lead check only: it reads the opening, it does
  not prove the flex came first semantically.)

Every chapter in the style:

- **finale-ending** — none of these in the last 2 sentences: `and so ends`,
  `that's the end of`, `that is the end of`, `and so concludes`,
  `so concludes the`, `the end of our story`, `and with that, our story`,
  `until next time`, `see you next time`, `see you in the next`,
  `thanks for watching`.
- **restart-opening** — none of these in the first 3 sentences:
  `welcome back`, `welcome to another`, `previously on`,
  `in the last chapter`, `in the previous chapter`, `last time on`,
  `as we saw last time`, `as we saw in the last`.
- **camera-cue / camera-direction** — no bracketed direction lines (only
  `[HOOK]`, `[LOOP: ...]`, `[music]`, `[Panel: ...]`, `[Beat ...]` cues are
  allowed) and no spoken sentence opening as a camera direction
  (`camera...`, `cut to...`, `close-up on...`, `zoom in/out...`, `pan to...`).
- **cue-form** — a sentence that looks like a standalone timeline cue
  (starts with Age/Week/Month/Year/Day, or "N units later") must parse with
  `detect_title_cue` from `manhwa_ocr/video/titlecards.py` (imported, not
  mirrored, so the two can never drift).

Panel-tag coverage is *not* rechecked here — the finalize path's `cues.check`
already owns it (errors block the save; tagged-ratio warnings ride along).
