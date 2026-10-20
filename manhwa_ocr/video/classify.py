"""Classify a narration sentence: should it even try to show a new panel?

Not every sentence describes something a panel can show. A sentence about a
character's unspoken thought, or about a sound with no visible actor, has no
picture to match - forcing a match anyway is how an unrelated panel (a vampire
romance scene for "the creature chirps in his head") ends up on screen. This
module decides, from the sentence text alone, whether panel-matching should
even be attempted:

    INTERNAL       - a thought, said to no one, under the breath, in the mind
    PURE_AUDIO     - a sound described as its own subject ("a roar echoes..."),
                     with no visible actor performing it in this sentence
    VISUAL_ACTION  - a character or creature visibly doing something, even if
                     that something is making a sound ("the monster roars")

Only VISUAL_ACTION sentences go to candidate-panel scoring. INTERNAL and
PURE_AUDIO hold whatever panel was already on screen.

This is deliberately simple pattern matching, not a model: it must be
reproducible and testable, and the categories it has to tell apart are
narrow enough that rules hold up (verified against tests/test_align.py).
"""

from __future__ import annotations

import re
from typing import Tuple

INTERNAL_PATTERNS = [
    (re.compile(r"\bin (?:his|her|their|its) (?:own )?(?:head|mind|thoughts)\b", re.I), "in his/her head"),
    (re.compile(r"\bto (?:himself|herself|themselves|itself)\b", re.I), "to himself/herself"),
    (re.compile(r"\bthought to (?:himself|herself|themselves|itself)\b", re.I), "thought to himself/herself"),
    (re.compile(r"\bunder (?:his|her|their|its) breath\b", re.I), "under his/her breath"),
    # Excludes "she thought it/that/this was..." -- a common retrospective
    # comparison about something VISIBLE ("not the argument she thought it
    # was"), not a description of an unspoken internal thought. Ch4: this
    # false positive held the previous panel for ~15s.
    (re.compile(r"\b(?:he|she|they)\s+(?:thinks?|thought|mused?|muses|recalled?|recalls?|realiz(?:ed|es)|wonders?)\b"
               r"(?!\s+(?:it|that|this)\b)", re.I), "he/she thinks"),
    (re.compile(r"\binternally\b", re.I), "internally"),
    (re.compile(r"\bin (?:his|her|their|its) mind'?s eye\b", re.I), "in his/her mind's eye"),
    (re.compile(r"\b(?:silently|mutely) (?:tells?|begs?|pleads?|screams?|whispers?)\b", re.I), "silently tells/begs"),
]

_SOUND_NOUN = r"(?:roar|chirp|scream|growl|hiss|screech|shriek|cry|howl|whisper|murmur|buzz|wail|noise|sound|echo)"
_PROPAGATION_VERB = (r"(?:echo(?:es|ed)?|rings? out|pierces?|pierced|fills?|filled|resonates?|resonated|"
                     r"sounds?|sounded|breaks? the silence|broke the silence|cuts?|cut through)")

PURE_AUDIO_SUBJECT = re.compile(
    rf"^\s*(?:[A-Z][a-z]+[,\s]+)?(?:a|an|the)?\s*(?:\w+\s+){{0,2}}{_SOUND_NOUN}s?\b", re.I)
PURE_AUDIO_VERB = re.compile(_PROPAGATION_VERB, re.I)


def classify(text: str) -> Tuple[str, str]:
    """(narration_type, reason) for one sentence. narration_type is one of
    INTERNAL, PURE_AUDIO, VISUAL_ACTION."""
    text = text or ""
    for pat, reason in INTERNAL_PATTERNS:
        if pat.search(text):
            return "INTERNAL", f"matched phrase: {reason!r}"
    if PURE_AUDIO_SUBJECT.match(text) and PURE_AUDIO_VERB.search(text):
        return "PURE_AUDIO", "a sound is the sentence's own subject, with no visible actor performing it"
    return "VISUAL_ACTION", "default: describes a visible character/action"
