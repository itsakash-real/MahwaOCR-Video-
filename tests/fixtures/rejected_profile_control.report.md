# Chapter 1 (CONTROL: rejected profile) — conformance + quality report
style: powerfantasy · 436 spoken words · tagged ratio 100% (0 same-only sentence(s))

## Lint (conformance)
- PASS (all gates green, exactly as the rejected pass was)

## Quality signals (measured)
| signal | value | bar | status |
|---|---|---|---|
| length | 436 | >= 560 words | warn |
| chained_sentences | 0.0 | >= 20.0% | warn |
| long_sentences | 0.0 | >= 5.0% | warn |
| vague_power_claims | 4 | <= 1 | warn |
| witness_reactions | 2.7 | >= 5.0/100 sentences | warn |
| persona_density | 0.0 | info (gate applies to manhwabro) | info |
| median_words_per_sentence | 6 | info | info |

- WARN length: under the 560-920 word band for a chapter
- WARN chained_sentences: almost no sentence carries 2+ story beats — reads as a written summary, not narration (rejected-pass profile)
- WARN long_sentences: no run-on sentences at all — the rhythm is flat one-beat lines
- WARN vague_power_claims: generic strength claims ('incredibly strong') instead of concrete proof
- WARN witness_reactions: power is proven through other people's shock (the hype-man rule); almost none here

## Chapter join (seam)
- opening: "An old man is dying in the snow."
- ending: "The chapter ends there."
- ends on question: False; ends on escalation word: False
- the last sentence is neither a question nor an escalation beat — check the join into the next chapter by ear

## Downstream contracts
- TTS-readiness: no punctuation conflicts
- panel usability: no known mask-risk/text-only tags

## Claim check (grounding)
- supported: 73 · flagged: 0 · no checkable entities (review): 0
- full sentence-by-sentence list: recaps/chapter_1 (CONTROL: rejected profile).claims.json

_Quality signals are measured warnings, not taste judgments; the numbers and thresholds are validated against the control corpus in tests/fixtures/rejected_profile_control.md (see docs/RECAP_SYSTEM_MAP.md)._
