"""Mechanical pre-synthesis checks on a chapter's narration sentences.

These catch the specific punctuation/wording patterns that are known, concrete
causes of a bad Deepgram Aura read -- a wandering em-dash/ellipsis standing in
for a sentence break, stacked punctuation, an unterminated sentence, raw
digits (Aura's number reading is inconsistent), and run-on sentences where
prosody degrades. None of this is a taste judgment: every check here is a
regex over the text, not a model call, so it runs before any API cost is
spent and before a human or an agent has to listen to find out something is
wrong.

``video tts`` runs this before synthesizing and refuses the WHOLE chapter if
anything is flagged (same "nothing applies until every issue is fixed"
pattern as ``fixes.json`` validation) -- fix the sentence in the recap and
re-finalize, then retry.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

_DIGIT = re.compile(r"\d")
_DASH_PAUSE = re.compile(r"\s[-–—]{1,2}\s|--")
_ELLIPSIS = re.compile(r"\.\.\.|…")
_DOUBLE_PUNCT = re.compile(r"[!?.,]{2,}")
_TERMINAL = re.compile(r"[.!?][\"'’”]?$")
_WORD = re.compile(r"[A-Za-z0-9']+")
RUN_ON_WORDS = 30


@dataclass
class LintIssue:
    n: int
    code: str
    message: str
    fix: str
    text: str


def _quote_balance_issue(text: str) -> str | None:
    straight = text.count('"')
    if straight % 2:
        return 'An unmatched " quote mark can make the sentence read as unterminated.'
    opens = text.count("“")
    closes = text.count("”")
    if opens != closes:
        return "Mismatched curly quotes (“ vs ”)."
    return None


def lint_sentence(n: int, text: str) -> list[LintIssue]:
    """All issues found in one sentence. Empty list means it's clean."""
    issues: list[LintIssue] = []
    text = text.strip()
    if not text:
        return issues

    if _DIGIT.search(text):
        issues.append(LintIssue(
            n, "digits",
            "Contains a raw digit; Aura's number reading is inconsistent.",
            "Spell the number out in words.", text))

    if _DASH_PAUSE.search(text) or "--" in text:
        issues.append(LintIssue(
            n, "dash-pause",
            "Uses a dash as a sentence-break stand-in; Aura reads a wandering "
            "dash as a stumble or an unnatural pause.",
            "Replace with a period or comma, or rewrite as two sentences.", text))

    if _ELLIPSIS.search(text):
        issues.append(LintIssue(
            n, "ellipsis",
            "Contains an ellipsis; prosody around it is unreliable.",
            "Replace with a period, or rewrite the trailing clause out.", text))

    if _DOUBLE_PUNCT.search(text):
        issues.append(LintIssue(
            n, "double-punct",
            "Stacked punctuation (e.g. '!?', '..', ',.').",
            "Reduce to a single mark.", text))

    if not _TERMINAL.search(text):
        issues.append(LintIssue(
            n, "unterminated",
            "Sentence has no terminal punctuation (. ! ?).",
            "Add a terminal mark so the synthesizer knows the sentence ended.", text))

    quote_msg = _quote_balance_issue(text)
    if quote_msg:
        issues.append(LintIssue(n, "quotes", quote_msg, "Balance or remove the quote marks.", text))

    word_count = len(_WORD.findall(text))
    if word_count > RUN_ON_WORDS:
        issues.append(LintIssue(
            n, "run-on",
            f"{word_count} words; Aura's prosody degrades on long run-on sentences.",
            f"Split into two sentences (keep each under ~{RUN_ON_WORDS} words).", text))

    return issues


def lint_sentences(sentences: list[str]) -> list[LintIssue]:
    """All issues across every sentence, in sentence order."""
    issues: list[LintIssue] = []
    for i, text in enumerate(sentences, start=1):
        issues.extend(lint_sentence(i, text))
    return issues


def format_report(issues: list[LintIssue]) -> str:
    lines = [f"{len(issues)} TTS-readiness issue(s) found -- fix all of them in the recap, "
             "then re-finalize before synthesizing:"]
    for issue in issues:
        snippet = issue.text if len(issue.text) <= 70 else issue.text[:67] + "..."
        lines.append(f"  sentence {issue.n} [{issue.code}]: {issue.message} Fix: {issue.fix}\n"
                     f"    \"{snippet}\"")
    return "\n".join(lines)
