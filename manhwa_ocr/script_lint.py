"""Mechanical style checks for Power Fantasy recaps (recap.style: powerfantasy).

The Power Fantasy system (docs/RECAP_STYLE_POWERFANTASY.md) has structural
rules a regex can hold the writer to: the range opener must run the hook
formula and the lore drop and must not lead with the past life; NO chapter in
the style may end on finale language or open on a restart, because the
chapter videos are joined into one continuous film; the spoken text carries
no camera directions; and a timeline cue sentence must be in a form the
video's title-card generator (manhwa_ocr/video/titlecards.py) actually
detects. Like video/tts_lint.py, every check here is a pattern over the text,
not a taste judgment and not a model call, so it can run in the recap
finalize path before any audio exists: a chapter that fails is not saved.

Limits, honestly stated: these are lead/pattern checks, not semantic proof.
The past-life check only reads the first few sentences (the rule is "do not
START with the past"); the hook check matches the formula's shape, not the
quality of the flex; the cue check only judges sentences that LOOK like
standalone cues (sentence-initial Age/Week/Month/Year/Day or "N units
later") — a time skip buried mid-sentence is the prompt's business, not
this gate's. Panel-tag coverage is NOT rechecked here: the finalize path's
cues.check already owns it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import List

from .video.cues import strip_tags
from .video.titlecards import detect_title_cue

# The system's opening formula: "This guy is [Name], a [low status role] at
# [prestigious place], but..."
HOOK_FORMULA_RE = re.compile(
    r"this guy is\s+.+?,\s*a\s+.+?\s+at\s+.+?,\s*but\b", re.IGNORECASE | re.DOTALL)
HOOK_SEARCH_SENTENCES = 8     # the formula lands inside the first minute
OPENING_SENTENCES = 3         # "the start" for the past-life / restart checks
ENDING_SENTENCES = 2          # "the end" for the finale check

# The lore drop's exact opening phrase (system rule 2).
LORE_PHRASE = "but what none of them know is"

# Lead-with-the-past markers (system rule 1: never open on these). Substrings,
# matched against the opening sentences only.
PAST_LIFE_MARKERS = (
    "reincarnat", "past life", "previous life", "former life", "was reborn",
    "transmigrat", "soul entered", "before he died", "after his death",
    "his childhood", "her childhood", "as a child", "when he was young",
    "when she was young",
)

# Closing/finale language banned at any chapter end in this style: the film
# continues into the next chapter, so a chapter ends mid-escalation.
FINALE_PHRASES = (
    "and so ends", "that's the end of", "that is the end of",
    "and so concludes", "so concludes the", "the end of our story",
    "and with that, our story", "until next time", "see you next time",
    "see you in the next", "thanks for watching",
)

# Restart language banned at any chapter opening in this style: no
# "welcome back", no previously-on — the narration flows straight in.
RESTART_PHRASES = (
    "welcome back", "welcome to another", "previously on",
    "in the last chapter", "in the previous chapter", "last time on",
    "as we saw last time", "as we saw in the last",
)

# Bracketed lines that are legitimate editing cues in this pipeline; any other
# bracketed line naming a camera move is a visual direction, which the system
# forbids in the script.
_ALLOWED_CUE_RE = re.compile(r"^\[(HOOK|LOOP\b|music|Panel:|Beat\b)", re.IGNORECASE)
_BRACKET_LINE_RE = re.compile(r"^\[.*\]$")
_DIRECTION_WORD_RE = re.compile(
    r"\b(camera|close[- ]?up|zoom|pan|tilt|tracking shot|wide shot|aerial shot)\b", re.IGNORECASE)
_DIRECTION_START_RE = re.compile(
    r"^(camera|cut to|close[- ]?up on|zoom (in|out)|pan (to|across|over|down|up))\b", re.IGNORECASE)

_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+")

# A sentence that is TRYING to be a standalone timeline cue: it starts with a
# timeline unit plus something, or with "N units later". Whether it succeeds
# is not this module's opinion — detect_title_cue decides.
_CUE_INTENT_RE = re.compile(
    r"^(age|week|month|year|day)\s+\S+|^\S+\s+(weeks?|months?|years?|days?)\s+later\b",
    re.IGNORECASE)


@dataclass
class LintIssue:
    code: str
    message: str
    fix: str
    text: str = ""


def _spoken_lines(narration: str) -> List[str]:
    """Narration lines with panel tags stripped (bracketed cue lines kept —
    the purity check needs to see them)."""
    out = []
    for raw in (narration or "").splitlines():
        line = strip_tags(raw).strip()
        if line:
            out.append(line)
    return out


def _sentences(lines: List[str]) -> List[str]:
    spoken = " ".join(l for l in lines if not _BRACKET_LINE_RE.match(l))
    return [s.strip() for s in _SENTENCE_SPLIT_RE.split(spoken) if s.strip()]


def lint_powerfantasy(narration: str, *, range_opener: bool) -> List[LintIssue]:
    """All style issues in one chapter's narration. Empty list means clean.

    `range_opener` is True only for the first chapter of a range (with no
    earlier recap on disk): the hook/lore/past-life checks apply to it alone.
    The continuity, purity and cue checks apply to every chapter.
    """
    issues: List[LintIssue] = []
    lines = _spoken_lines(narration)
    sentences = _sentences(lines)
    if not sentences:
        return issues
    opening = " ".join(sentences[:OPENING_SENTENCES])
    ending = " ".join(sentences[-ENDING_SENTENCES:])
    full = " ".join(sentences)

    if range_opener:
        hook_region = " ".join(sentences[:HOOK_SEARCH_SENTENCES])
        if not HOOK_FORMULA_RE.search(hook_region):
            issues.append(LintIssue(
                "hook-formula",
                "The range opener never runs the hook formula "
                "(\"This guy is [Name], a [low status role] at [prestigious place], but...\").",
                "Open in medias res with the formula and the effortless payoff inside "
                "the first 30 seconds.", hook_region[:120]))
        if LORE_PHRASE not in full.lower():
            issues.append(LintIssue(
                "lore-drop",
                "The range opener never drops the lore: the phrase "
                "\"But what none of them know is...\" is missing.",
                "After the opening flex, open the lore drop with that exact phrase.", ""))
        marker = next((m for m in PAST_LIFE_MARKERS if m in opening.lower()), None)
        if marker:
            issues.append(LintIssue(
                "past-life-opening",
                f"The opener leads with the past (\"{marker}\") before the flex; "
                "the system forbids starting with the MC's past, reincarnation or childhood.",
                "Move the backstory behind the hook, into the lore drop.", opening[:120]))

    phrase = next((p for p in FINALE_PHRASES if p in ending.lower()), None)
    if phrase:
        issues.append(LintIssue(
            "finale-ending",
            f"The chapter ends on finale language (\"{phrase}\"); chapters in this style "
            "are joined into one film and must end mid-escalation.",
            "Cut the closing line; end on the beat that pulls into the next chapter.",
            ending[:120]))

    phrase = next((p for p in RESTART_PHRASES if p in opening.lower()), None)
    if phrase:
        issues.append(LintIssue(
            "restart-opening",
            f"The chapter opens with restart language (\"{phrase}\"); the narration must "
            "flow straight in from the previous chapter.",
            "Open mid-flow on the chapter's strongest beat instead.", opening[:120]))

    for line in lines:
        if _BRACKET_LINE_RE.match(line) and not _ALLOWED_CUE_RE.match(line) \
                and _DIRECTION_WORD_RE.search(line):
            issues.append(LintIssue(
                "camera-cue",
                f"A bracketed line gives a camera direction (\"{line}\"); the script is "
                "pure voiceover — visual directions are forbidden.",
                "Delete the direction; keep only [HOOK] / [LOOP: ...] / [Panel: ...] cues.",
                line))
    for s in sentences:
        if _DIRECTION_START_RE.match(s):
            issues.append(LintIssue(
                "camera-direction",
                f"A spoken sentence opens as a camera direction (\"{s[:80]}\"); the "
                "narration is only the exact words the narrator speaks.",
                "Rewrite it as narration, or delete it.", s[:120]))

    for s in sentences:
        if _CUE_INTENT_RE.match(s) and detect_title_cue(s) is None:
            issues.append(LintIssue(
                "cue-form",
                f"A timeline cue the title-card generator cannot detect: \"{s[:80]}\". "
                "Its accepted forms come from manhwa_ocr/video/titlecards.py.",
                "Write the skip as a standalone sentence in a detected form, e.g. "
                "\"Age fifteen.\" / \"Week six of possession.\" / \"Three years later.\"",
                s[:120]))

    return issues


def format_report(issues: List[LintIssue]) -> str:
    lines = [f"{len(issues)} power-fantasy style issue(s) found -- fix all of them in the "
             "recap, then re-finalize:"]
    for issue in issues:
        snippet = issue.text if len(issue.text) <= 70 else issue.text[:67] + "..."
        lines.append(f"  [{issue.code}]: {issue.message} Fix: {issue.fix}"
                     + (f"\n    \"{snippet}\"" if snippet else ""))
    return "\n".join(lines)
