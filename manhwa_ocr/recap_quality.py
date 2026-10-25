"""Measured quality signals for recap scripts (conformance reports what the
script LOOKS like; this measures how it READS).

Every signal here was validated on 2026-10-24 against real corpora before
being given a threshold (docs/RECAP_SYSTEM_MAP.md, Part 3):

  - the rejected-profile control (tests/fixtures/rejected_profile_control.md,
    built from the incident log's post-mortem of the rejected power-fantasy
    pass: one event per sentence, generic strength claims, almost no witness
    reactions, no narrator persona) — must be FLAGGED;
  - the reference-channel anchors (tests/test_manhwabro.py OPENER /
    CONTINUATION) and every recap then on disk (9 series, 3210 cue
    sentences) — must PASS.

Signals that separated cleanly became warnings with thresholds; signals
that did not separate (number density) or had no corpus to validate
against (speedrun pivot density) are reported as INFO only. What no regex
can measure — whether a joke lands, whether the flex feels effortless —
stays a review step; nothing here pretends otherwise.

Thresholds are pinned constants (the HARDCODE rule: style decisions live in
versioned code, not in an agent's head). `recap.min_words` / `recap.max_words`
in manhwa-ocr.yaml override the length band.
"""

from __future__ import annotations

import re
import statistics
from dataclasses import dataclass, field
from typing import Any, Dict, List

# ---- pinned thresholds (validated 2026-10-24; see module docstring) -------
# MIN/MAX_WORDS: the report's word-band WARNs, matched to the narration
# length CONTRACT (2026-10-25 user ruling: 6-8 FINISHED minutes; finalize
# refuses outside it, speed-aware — see script._length_contract_errors).
# 6 min x 150 wpm x 1.3 speed = 1170; 8 x 150 x 1.3 = 1560.
MIN_WORDS = 1170
MAX_WORDS = 1560
CHAINED_MIN_PCT = 20.0   # validated: control 0.0 vs real recaps 24.2-84.1
LONG_SENT_MIN_PCT = 5.0  # validated: control 0.0 vs real recaps 7.6-79.5
VAGUE_MAX = 1            # validated: control 4 vs real 0 / reference 0
REACT_MIN_PER_100 = 5.0  # manhwabro + powerfantasy lanes (the hype-man rule)
PERSONA_MIN_PER_100 = 5.0  # manhwabro lane (control 0.0 vs reference 83-133)

_CONNECT_RE = re.compile(
    r",|\band\b|\bbut\b|\bthen\b|\bso\b|\bonly to\b|\bonly for\b|\bwho\b|"
    r"\bwhich\b|\bwhile\b|\bbefore\b|\bafter\b|\bwhen\b|;|—", re.IGNORECASE)
_LONG_WORDS = 18
_VAGUE_RE = re.compile(
    r"\b(incredibly|unbelievably|immensely|extremely|super|very|really)\s+"
    r"(strong|powerful|fast|good|skilled|weak)\b"
    r"|\bso\s+(strong|powerful)\b"
    r"|\bamazing\s+(strength|power|skill)", re.IGNORECASE)
_REACT_RE = re.compile(
    r"\b(stare[sd]?|staring|stunned|shock\w*|gasp\w*|frozen|speechless|"
    r"trembl\w*|panic\w*|terrif\w*|jaw\b|eyes?\s+wid\w+|pale\b|sweat\w*|"
    r"silence|silent|screams?|screamed|shouts?|shouted|backing away|"
    r"couldn'?t believe|can'?t believe|awed|awe\b|horror|disbelief)\b", re.IGNORECASE)
_PERSONA_RE = re.compile(
    r"\bour (boy|guy|man|protagonist|cool)\b|\bthis (brat|man|guy|kid|dude)\b|"
    r"\bbro\b|\bthe young master\b|\bclown\b|\bold fart\b|\bpoor bastard\b|"
    r"\bpoor guy\b|\babsolute amateur\b", re.IGNORECASE)
_RHETORICAL_Q_RE = re.compile(r"\b(why|how)\b[^.?!]*\?", re.IGNORECASE)

# Continuity phrase lists are owned by script_lint (single source, no drift).
from .script_lint import FINALE_PHRASES, RESTART_PHRASES  # noqa: E402


@dataclass
class Signal:
    name: str
    value: float
    threshold: str            # human description of the bar
    status: str               # "ok" | "warn" | "info"
    note: str = ""


def measure_sentences(sentences: List[str]) -> Dict[str, Any]:
    """Raw signal values for one chapter's spoken sentences."""
    n = len(sentences) or 1
    words = sum(len(s.split()) for s in sentences)
    per_sentence = [len(s.split()) for s in sentences]
    chained = sum(1 for s in sentences if len(_CONNECT_RE.findall(s)) >= 2)
    long_s = sum(1 for s in sentences if len(s.split()) >= _LONG_WORDS)

    def vague_hits(s: str) -> int:
        # A rhetorical pivot question ("So, why is this brat so strong?") is
        # the lane's signature, not a vague claim: exclude why/how questions.
        stripped = _RHETORICAL_Q_RE.sub("", s)
        return len(_VAGUE_RE.findall(stripped))

    return {
        "sentences": len(sentences),
        "words": words,
        "median_words_per_sentence": round(statistics.median(per_sentence), 1) if per_sentence else 0,
        "chained_pct": round(100.0 * chained / n, 1),
        "long_sentence_pct": round(100.0 * long_s / n, 1),
        "vague_power_claims": sum(vague_hits(s) for s in sentences),
        "reaction_beats_per_100": round(100.0 * sum(len(_REACT_RE.findall(s)) for s in sentences) / n, 1),
        "persona_marks_per_100": round(100.0 * sum(len(_PERSONA_RE.findall(s)) for s in sentences) / n, 1),
    }


def signals(style: str, m: Dict[str, Any], *, min_words: int = MIN_WORDS,
            max_words: int = MAX_WORDS) -> List[Signal]:
    """Lane-aware signal table: warnings only where validation proved the
    signal separates the rejected profile from good scripts."""
    out: List[Signal] = []
    w = m["words"]
    if w < min_words:
        out.append(Signal("length", w, f">= {min_words} words", "warn",
                          f"under the {min_words}-{max_words} word band for a chapter"))
    elif w > max_words:
        out.append(Signal("length", w, f"<= {max_words} words", "warn",
                          "over the band: owner ruling is that longer scripts over-explain"))
    else:
        out.append(Signal("length", w, f"{min_words}-{max_words} words", "ok"))

    if m["chained_pct"] < CHAINED_MIN_PCT:
        out.append(Signal("chained_sentences", m["chained_pct"], f">= {CHAINED_MIN_PCT}%", "warn",
                          "almost no sentence carries 2+ story beats — reads as a written summary, "
                          "not narration (rejected-pass profile)"))
    else:
        out.append(Signal("chained_sentences", m["chained_pct"], f">= {CHAINED_MIN_PCT}%", "ok"))

    if m["long_sentence_pct"] < LONG_SENT_MIN_PCT:
        out.append(Signal("long_sentences", m["long_sentence_pct"], f">= {LONG_SENT_MIN_PCT}%", "warn",
                          "no run-on sentences at all — the rhythm is flat one-beat lines"))
    else:
        out.append(Signal("long_sentences", m["long_sentence_pct"], f">= {LONG_SENT_MIN_PCT}%", "ok"))

    if m["vague_power_claims"] > VAGUE_MAX:
        out.append(Signal("vague_power_claims", m["vague_power_claims"], f"<= {VAGUE_MAX}", "warn",
                          "generic strength claims ('incredibly strong') instead of concrete proof"))
    else:
        out.append(Signal("vague_power_claims", m["vague_power_claims"], f"<= {VAGUE_MAX}", "ok"))

    if style in ("manhwabro", "powerfantasy"):
        if m["reaction_beats_per_100"] < REACT_MIN_PER_100:
            out.append(Signal("witness_reactions", m["reaction_beats_per_100"],
                              f">= {REACT_MIN_PER_100}/100 sentences", "warn",
                              "power is proven through other people's shock (the hype-man rule); "
                              "almost none here"))
        else:
            out.append(Signal("witness_reactions", m["reaction_beats_per_100"],
                              f">= {REACT_MIN_PER_100}/100 sentences", "ok"))
    else:
        out.append(Signal("witness_reactions", m["reaction_beats_per_100"],
                          "info (gate applies to manhwabro/powerfantasy)", "info"))

    if style == "manhwabro":
        if m["persona_marks_per_100"] < PERSONA_MIN_PER_100:
            out.append(Signal("persona_density", m["persona_marks_per_100"],
                              f">= {PERSONA_MIN_PER_100}/100 sentences", "warn",
                              "the bro persona is absent — epithets and roasts are the voice"))
        else:
            out.append(Signal("persona_density", m["persona_marks_per_100"],
                              f">= {PERSONA_MIN_PER_100}/100 sentences", "ok"))
    else:
        out.append(Signal("persona_density", m["persona_marks_per_100"],
                          "info (gate applies to manhwabro)", "info"))

    # INFO-only signals: measured, not gated (see module docstring).
    out.append(Signal("median_words_per_sentence", m["median_words_per_sentence"], "info", "info"))
    return out


# --------------------------------------------------------------- joins ----

_ESCALATION_RE = re.compile(r"\b(but|then|until|suddenly|now|before)\b", re.IGNORECASE)


def join_signals(sentences: List[str], style: str) -> Dict[str, Any]:
    """Seam check between chapters: the keyword gates (finale/restart) plus a
    shape read of the first/last sentence. Shape reads are advisory — a
    human still listens to the join."""
    if not sentences:
        return {}
    first, last = sentences[0], sentences[-1]
    opening = " ".join(sentences[:3])
    ending = " ".join(sentences[-2:])
    restart = next((p for p in RESTART_PHRASES if p in opening.lower()), None)
    finale = next((p for p in FINALE_PHRASES if p in ending.lower()), None)
    return {
        "restart_phrase": restart,
        "finale_phrase": finale,
        "ends_on_question": last.strip().endswith("?"),
        "ends_on_escalation": bool(_ESCALATION_RE.search(last)),
        "first_words": first[:80],
        "last_words": last[:80],
    }


def join_warnings(j: Dict[str, Any]) -> List[str]:
    out = []
    if j.get("restart_phrase"):
        out.append(f"the opening uses restart language (\"{j['restart_phrase']}\"); "
                   "the film must flow straight in from the previous chapter")
    if j.get("finale_phrase"):
        out.append(f"the ending uses finale language (\"{j['finale_phrase']}\"); "
                   "end mid-escalation instead")
    if not j.get("ends_on_question") and not j.get("ends_on_escalation"):
        out.append("the last sentence is neither a question nor an escalation beat — "
                   "check the join into the next chapter by ear")
    return out


# ----------------------------------------------------- downstream contracts

def tts_readiness_warnings(sentences: List[str]) -> List[str]:
    """Incident 6: the recap linters and the TTS linter disagree about
    punctuation. Surface the TTS stage's verdict at WRITING time so a script
    that would be refused at synthesis is caught while it is still cheap to
    fix. Advisory — lanes may legitimately use em-dashes (the owner has not
    ruled); the point is the contract is now visible in one report."""
    from .video.tts_lint import lint_sentences

    issues = lint_sentences(list(sentences))
    if not issues:
        return []
    by_code: Dict[str, List[Any]] = {}
    for i in issues:
        by_code.setdefault(i.code, []).append(i)
    out = []
    for code, items in sorted(by_code.items()):
        first = items[0]
        out.append(f"{len(items)} sentence(s) fail the TTS-readiness check '{code}' "
                   f"(first: sentence {first.n}: \"{first.text[:60]}\"). The synthesis stage "
                   f"refuses these. Fix: {first.fix}")
    return out


def panel_usability_warnings(tagged_panels: List[str], panels: List[Dict[str, Any]],
                             workdir, chapter: str) -> List[str]:
    """Incident 9: the video stage drops some tagged panels (mask-risk from
    the QA file once a plan exists; text/credits rows always). Warn at
    writing time instead of surprising the build."""
    out: List[str] = []
    by_id = {p["id"]: p for p in panels}
    mask_risk: List[str] = []
    try:
        import json

        qa = workdir / "video" / f"chapter_{chapter}.qa.json"
        if qa.exists():
            data = json.loads(qa.read_text(encoding="utf-8"))
            mask_risk = [str(p) for p in (data.get("mask_risk") or [])]
    except (OSError, ValueError):
        mask_risk = []
    risky = [pid for pid in dict.fromkeys(tagged_panels) if pid in mask_risk]
    if risky:
        out.append("tagged panel(s) " + ", ".join(risky) +
                   " are mask-risk (a dominant bubble over the head range): the video stage "
                   "DROPS them — re-tag those sentences to a clean panel (see video/chapter_"
                   f"{chapter}.qa.json)")
    textish = [pid for pid in dict.fromkeys(tagged_panels)
               if pid in by_id and (by_id[pid].get("text_only") or by_id[pid].get("credits"))]
    if textish:
        out.append("tagged panel(s) " + ", ".join(textish) + " are text-only/credits (hard error "
                   "at finalize; listed here for the report)")
    return out


# ------------------------------------------------------------- the report

@dataclass
class ChapterReport:
    chapter: str
    style: str
    lint_codes: List[str] = field(default_factory=list)
    lint_lines: List[str] = field(default_factory=list)
    tagged_ratio: float = 0.0
    same_sentences: int = 0
    signals: List[Signal] = field(default_factory=list)
    joins: Dict[str, Any] = field(default_factory=dict)
    join_warnings: List[str] = field(default_factory=list)
    tts_warnings: List[str] = field(default_factory=list)
    panel_warnings: List[str] = field(default_factory=list)
    claims_summary: Dict[str, Any] = field(default_factory=dict)
    words: int = 0

    def markdown(self) -> str:
        lines = [f"# Chapter {self.chapter} — conformance + quality report",
                 f"style: {self.style} · {self.words} spoken words · "
                 f"tagged ratio {self.tagged_ratio:.0%} ({self.same_sentences} same-only sentence(s))",
                 ""]
        lines.append("## Lint (conformance)")
        if self.lint_lines:
            lines += [f"- {l}" for l in self.lint_lines]
        elif self.lint_codes == []:
            lines.append("- PASS (no style-lint issues)")
        lines.append("")
        lines.append("## Quality signals (measured)")
        lines.append("| signal | value | bar | status |")
        lines.append("|---|---|---|---|")
        for s in self.signals:
            lines.append(f"| {s.name} | {s.value} | {s.threshold} | {s.status} |")
        warns = [s for s in self.signals if s.status == "warn"]
        if warns:
            lines.append("")
            lines += [f"- WARN {s.name}: {s.note}" for s in warns]
        lines.append("")
        lines.append("## Chapter join (seam)")
        j = self.joins
        if j:
            lines.append(f"- opening: \"{j.get('first_words', '')}\"")
            lines.append(f"- ending: \"{j.get('last_words', '')}\"")
            lines.append(f"- ends on question: {bool(j.get('ends_on_question'))}; "
                         f"ends on escalation word: {bool(j.get('ends_on_escalation'))}")
        lines += [f"- {w}" for w in self.join_warnings] or ["- no join warnings"]
        lines.append("")
        lines.append("## Downstream contracts")
        if self.tts_warnings:
            lines += [f"- {w}" for w in self.tts_warnings]
        else:
            lines.append("- TTS-readiness: no punctuation conflicts")
        if self.panel_warnings:
            lines += [f"- {w}" for w in self.panel_warnings]
        else:
            lines.append("- panel usability: no known mask-risk/text-only tags")
        cs = self.claims_summary
        if cs:
            lines.append("")
            lines.append("## Claim check (grounding)")
            lines.append(f"- supported: {cs.get('supported', 0)} · flagged: {cs.get('flagged', 0)} · "
                         f"no checkable entities (review): {cs.get('no_checkable_tokens', 0)}")
            for f in (cs.get("flagged_examples") or [])[:5]:
                lines.append(f"  - FLAG sentence {f.get('n')}: {f.get('flags', ['?'])[0] if f.get('flags') else '?'} "
                             f"— \"{(f.get('text') or '')[:70]}\"")
            lines.append(f"- full sentence-by-sentence list: recaps/chapter_{self.chapter}.claims.json")
        lines.append("")
        lines.append("_Quality signals are measured warnings, not taste judgments; the numbers "
                     "and thresholds are validated against the control corpus in "
                     "tests/fixtures/rejected_profile_control.md (see docs/RECAP_SYSTEM_MAP.md)._")
        return "\n".join(lines) + "\n"
