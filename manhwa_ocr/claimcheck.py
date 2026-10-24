"""Rules-based claim check: is every recap sentence grounded in its chapter
transcript (or the series bible)?

No model calls, no API: entity/number/quote matching only, designed to be
false-alarm-averse. Per sentence it returns one of:

  supported          at least one checkable token (name/number/quote) matched
                     the transcript or bible
  flagged            a name that appears nowhere in the transcript or bible,
                     a number that does, or a long quote with no transcript
                     counterpart
  no-checkable-tokens pure commentary (nothing checkable) — a human reviews
                     these; rules cannot judge a paraphrase's truth

What this CANNOT catch (said plainly): a wrong verb ("he kills" vs "he
spares"), an upgraded feat phrased without names or numbers, an invented
event described in generic words. Those stay review steps; the claim-check
report lists every sentence with its evidence so the review is fast.

Written for incident 10; wired into the recap finalize path, which saves
recaps/chapter_N.claims.json next to the recap.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

from .video.cues import spell_numbers

# Capitalised sequences in the RECAP: 1-3 words, letters/'- only.
_CAP_SEQ_RE = re.compile(r"\b[A-Z][a-z'’\-]+(?:\s+[A-Z][a-z'’\-]+){0,2}\b")
_CAP_WORD_RE = re.compile(r"\b[A-Z][a-z'’\-]+\b")
_ALLCAPS_RE = re.compile(r"\b[A-Z]{2,}s?\b")
# A dialogue line in a transcript: **Speaker:** "TEXT" (thinking variants too).
_DIALOGUE_RE = re.compile(r"^\*\*.+?\*\*:")
_SPEAKER_RE = re.compile(r"^\*\*(.+?)\*\*:")
_NUMBER_WORDS = sorted(set(
    "zero one two three four five six seven eight nine ten eleven twelve".split()
    + "thirteen fourteen fifteen sixteen seventeen eighteen nineteen".split()
    + "twenty thirty forty fifty sixty seventy eighty ninety".split()
    + "hundred thousand million billion".split()
    + "first second third fourth fifth sixth seventh eighth ninth tenth".split()))
_NUMBER_TOKEN_RE = re.compile(r"\b(" + "|".join(_NUMBER_WORDS) + r")\b", re.IGNORECASE)
_MAGITUDES = {"thousand", "million", "billion"}
_QUOTE_RE = re.compile(r'["“]([^"”\n]{25,400})["”]')
_SKIP_TOKENS = {"I", "Okay", "Ok", "Oh", "Well", "Yeah", "Hey", "Wait", "Look",
                "But", "And", "So", "Then", "Now", "Meanwhile", "Flashback"}


def _norm(text: str) -> str:
    return re.sub(r"[^a-z0-9\s]", " ", (text or "").casefold())


class TranscriptIndex:
    """Entity tokens, number tokens and quote n-grams from one transcript
    plus the bible as of the chapter."""

    def __init__(self, transcript: str, memory=None, upto: Optional[str] = None):
        self.entity_tokens: Set[str] = set()
        self.entity_ngrams: Set[str] = set()
        self.proper_tokens: Set[str] = set()     # words seen Capitalised in prose/bible
        self.number_tokens: Set[str] = set()
        self.line_tokens: List[List[str]] = []       # for quote/anchor line lookup
        self.magnitudes: Set[str] = set()
        spelled = spell_numbers(transcript or "")
        for raw_line in (transcript or "").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("[SFX"):
                continue
            if line.startswith("#"):
                self._add_entities(line.lstrip("#").strip())
                continue
            if _DIALOGUE_RE.match(line):
                speaker = _SPEAKER_RE.match(line)
                if speaker:
                    self._add_entities(speaker.group(1), proper=True)
            else:
                # Scene descriptions / narration prose: capitalised words in
                # mixed-case prose are the proper nouns ("Mikail", not "the")
                self._add_entities(line, proper=True)
            self.line_tokens.append(_norm(line).split())
        # numbers come from everything, dialogue included (facts live in
        # dialogue); both sides are digit-spelled first, so 40 == "forty"
        for token in _norm(spelled).split():
            if _NUMBER_TOKEN_RE.match(token):
                self.number_tokens.add(token)
                if token in _MAGITUDES:
                    self.magnitudes.add(token)
        if memory is not None:
            try:
                briefing = memory.as_briefing(upto=upto)
            except TypeError:
                briefing = memory.as_briefing()
            self._add_entities(briefing)
            for name, char in (getattr(memory, "characters", {}) or {}).items():
                self._add_entities(str(name), proper=True)
                for alias in (char.get("aliases") or []):
                    self._add_entities(str(alias), proper=True)
            for term in (getattr(memory, "glossary", {}) or {}):
                self._add_entities(str(term))
        # quote matching: 3-gram set over the whole transcript (spelled+normed)
        full = _norm(spelled).split()
        self.trigrams = {" ".join(full[i:i + 3]) for i in range(max(0, len(full) - 2))}

    def _add_entities(self, text: str, proper: bool = False) -> None:
        toks = _norm(text).split()
        self.entity_tokens.update(toks)
        for i in range(len(toks)):
            self.entity_ngrams.add(" ".join(toks[i:i + 3]))
        if proper:
            self.proper_tokens.update(_norm(w) for w in _CAP_WORD_RE.findall(text))

    def match_entity(self, seq: str) -> bool:
        toks = _norm(seq).split()
        if not toks:
            return True
        if " ".join(toks) in self.entity_ngrams:
            return True
        return all(t in self.entity_tokens for t in toks)

    def match_proper(self, seq: str) -> bool:
        """A sequence seen Capitalised in the transcript's prose or named in
        the bible (used for sentence-initial words, where ordinary words
        are capitalised too)."""
        toks = _norm(seq).split()
        if not toks:
            return False
        if " ".join(toks) in self.entity_ngrams:
            return True
        return all(t in self.proper_tokens for t in toks)

    def match_number(self, token: str) -> bool:
        t = token.casefold()
        if t in self.number_tokens:
            return True
        # a rounded magnitude ("two billion") is supported when the
        # transcript has any number of that magnitude at all
        return t in _MAGITUDES and t in self.magnitudes

    def match_quote(self, quote: str) -> float:
        toks = _norm(spell_numbers(quote)).split()
        if not toks:
            return 1.0
        grams = [" ".join(toks[i:i + 3]) for i in range(len(toks) - 2)]
        if not grams:
            return 1.0 if " ".join(toks) in self.trigrams else 0.0
        hit = sum(1 for g in grams if g in self.trigrams)
        return hit / len(grams)

    def lines_with(self, token: str) -> List[int]:
        t = token.casefold()
        return [i + 1 for i, toks in enumerate(self.line_tokens) if t in toks][:3]


def check_sentences(sentences: List[str], index: TranscriptIndex) -> Dict[str, Any]:
    rows: List[Dict[str, Any]] = []
    supported = flagged = no_tokens = 0
    for n, sentence in enumerate(sentences, start=1):
        flags: List[str] = []
        anchors: List[str] = []
        evidence_lines: List[int] = []

        spelled = spell_numbers(sentence)
        # names: capitalised sequences not at the sentence start (position 0
        # is where ordinary words are capitalised; skipping it keeps the
        # check false-alarm-averse) and ALL-CAPS terms
        body = sentence.strip()
        seqs = [m.group(0) for m in _CAP_SEQ_RE.finditer(body)]
        first = None
        if body:
            first = re.match(r"[A-Z][a-z'’\-]+(?:\s+[A-Z][a-z'’\-]+){0,2}", body)
            if first and first.group(0) in seqs:
                seqs.remove(first.group(0))
        for seq in seqs:
            if seq in _SKIP_TOKENS:
                continue
            if index.match_entity(seq):
                anchors.append(f"name: {seq}")
                evidence_lines += index.lines_with(seq.split()[0])
            else:
                flags.append(f"name not in transcript or bible: \"{seq}\"")
        # A sentence-initial name that IS a known proper noun counts as an
        # anchor; an unknown sentence-initial word is left alone (ordinary
        # sentence starters are capitalised too -- suppressing those keeps
        # the check false-alarm-averse).
        if first and first.group(0) not in _SKIP_TOKENS and index.match_proper(first.group(0)):
            anchors.append(f"name: {first.group(0)}")
            evidence_lines += index.lines_with(first.group(0).split()[0].casefold())
        for m in _ALLCAPS_RE.finditer(body):
            term = m.group(0)
            if index.match_entity(term):
                anchors.append(f"term: {term}")
            elif len(term) >= 3:
                flags.append(f"term not in transcript or bible: \"{term}\"")

        for m in _NUMBER_TOKEN_RE.finditer(spelled):
            tok = m.group(0)
            if index.match_number(tok):
                anchors.append(f"number: {tok}")
                evidence_lines += index.lines_with(tok)
            else:
                flags.append(f"number not found in transcript: \"{tok}\"")

        for m in _QUOTE_RE.finditer(body):
            ratio = index.match_quote(m.group(1))
            if ratio >= 0.5:
                anchors.append(f"quote ({ratio:.0%} n-gram match)")
            else:
                flags.append(f"long quote not grounded in transcript "
                             f"({ratio:.0%} n-gram match): \"{m.group(1)[:60]}\"")

        if flags:
            verdict = "flagged"
            flagged += 1
        elif anchors:
            verdict = "supported"
            supported += 1
        else:
            verdict = "no-checkable-tokens"
            no_tokens += 1
        rows.append({"n": n, "text": sentence, "verdict": verdict,
                     "anchors": anchors, "flags": flags,
                     "transcript_lines": sorted(set(evidence_lines))[:4]})
    flagged_rows = [r for r in rows if r["verdict"] == "flagged"]
    return {
        "sentences": rows,
        "summary": {
            "supported": supported,
            "flagged": flagged,
            "no_checkable_tokens": no_tokens,
            "flagged_examples": [
                {"n": r["n"], "text": r["text"], "flags": r["flags"]} for r in flagged_rows[:10]
            ],
        },
    }


def write_report(recaps_dir: Path, chapter: str, data: Dict[str, Any]) -> Path:
    import json

    from .util import atomic_write_text

    out = recaps_dir / f"chapter_{chapter}.claims.json"
    atomic_write_text(out, json.dumps(data, indent=1, ensure_ascii=False))
    return out
