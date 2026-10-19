# Recap Style: Speedrun Gossip

A narration style reverse-engineered from a high-engagement recap channel
(sample: the "sick leave denied → killed his boss" recap). It reads like a
friend spilling unbelievable gossip at 2x speed. Every sentence delivers a
payoff; there is no filler anywhere.

Enable per series in `manhwa-ocr.yaml`:

```yaml
recap:
  style: speedrun   # default: default (the calm present-tense voice)
```

This is a *different lane* from the Professional International persona
(present tense, repeated names, controlled cinematic tone). Do not mix them
in one video — pick one voice per series.

---

## 1. The mechanics (what the sample actually does)

**1. Chain-of-consequences sentences.**
One sentence carries 2–4 story beats linked by commas and relative clauses.
> "He asked for sick leave, but his request got denied. So, he killed his
> boss, having finally had enough, only to get caught by the company's
> partner, who offered him a job promising him full benefits."

**2. Pivot words are the engine: So / But / Only to / Only for.**
"So," opens sentences (casual momentum). "But" reverses. "Only to" /
"only for" subvert the expected payoff. Almost every sentence contains a
reversal — a mini-twist per sentence, not per minute.
> "But he also killed him after 10 days of unpaid overtime…"
> "…only to get arrested before reaching the main gate."
> "…only for him to throw up as they traveled by Eagle…"

**3. Role labels, not names.**
"his boss", "the company's partner", "a rival", "a commander", "her".
Zero proper nouns to memorize = zero cognitive load = effortless listening.
It also sounds like gossip ("you know, the boss").

**4. Editorial asides.**
The narrator judges, jokes, and psychoanalyzes mid-sentence:
> "having finally had enough"
> "got what he deserved"
> "which he wrongfully assumed meant she didn't believe he was strong enough"
> "leaving her a bit saddened"

**5. Past tense, breathless.**
"asked", "killed", "went", "realized". Past tense makes it feel like gossip
about something outrageous that already happened — "you won't believe what
he did."

**6. Zero setup, zero filler.**
No intro, no "in this chapter", no previously-on. Sentence one IS the
inciting incident. No sentence restates the previous one.

**7. `[music]` beat markers.**
Embedded where the energy shifts — editing cues for beat drops, never read
aloud. (The repo already supports `[music]`-style bracket cues; keep them on
their own lines.)

**8. Comedy from mundane + epic collision.**
> "got a new offer by a rival borrowing their toilet"
> "who called him on weekends and got what he deserved"
Small petty details next to murders and terrorist attacks = funny without
trying.

**9. Ends mid-escalation.**
The sample cuts at "which located the terrorists hiding in a nearby
building" — no resolution, just the next problem. The viewer is pulled
forward, never released.

## 2. Why it works

- Every sentence promises *and* delivers a payoff within ~10 seconds → a
  dopamine loop the viewer doesn't want to break.
- So/but/only-to = constant prediction error → attention stays locked.
- No names = no memory burden → works at 1.5x speed, works half-distracted.
- Gossip framing = social curiosity → "what did he do NEXT?"

## 3. The rules (for the writer — human or model)

DO:
- Open on the inciting incident. No greeting, no setup, no context-setting.
- Pack 2–4 beats into every sentence with commas, "who/where/which"
  clauses, and participles ("having finally had enough").
- Start sentences with "So," when continuing momentum; use "But" to pivot;
  use "only to" / "only for" when the payoff gets snatched away.
- Refer to characters by role ("the commander", "his boss", "her"), never
  by proper name unless the name itself is the joke or the plot point.
- Add one editorial aside per 2–3 sentences — a judgment, a joke, or a
  motive read ("which he took as a personal insult").
- Write in past tense.
- Drop `[music]` on its own line at energy shifts (kills, reveals, chases).
- End the chapter on a new problem, not a resolution.

DON'T:
- Don't write one-beat-per-sentence summaries ("He went to the gate. He
  was arrested. A commander came."). Chain them.
- Don't explain lore, systems, or backstory unless a beat requires it —
  and then compress it into a clause, not a paragraph.
- Don't use present tense, don't address the viewer ("you guys"), don't
  moralize.
- Don't quote long dialogue. At most a 5-word punchline quote per chapter.
- Don't invent events, characters, or dialogue. Speed comes from
  compression, never from fabrication.

## 4. Before / after (same beats, two voices)

Source beats: Hyeonyul's sick leave is denied → he kills his boss → the
company's partner catches him → offers a job with full benefits.

Default voice:
> "When his sick leave request is denied one time too many, Hyeonyul
> finally snaps and kills his boss. But the company's partner was watching —
> and instead of calling the police, he offers Hyeonyul a job with full
> benefits."

Speedrun gossip:
> "He asked for sick leave, but his request got denied. So, he killed his
> boss, having finally had enough, only to get caught by the company's
> partner, who offered him a job promising him full benefits."

Same facts. Half the words. Twice the momentum.

## 5. Panel tagging (unchanged)

The speedrun style changes the *voice*, not the machinery. Every sentence
still starts with its `{P003-02}`-style panel tag, `{same}` / `{beat}` /
`{impact}` / `{reveal}` work exactly as before, and the sync gate, QA, and
storyboard rules are untouched. See `manhwa_ocr/script.py` (`SPEEDRUN_SYSTEM`)
and `docs/manhwa_ocr_transcription_and_recap_guide.md`.
