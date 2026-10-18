"""Panel tags in recap narration.

The recap writer starts each sentence with the panel it describes:

    {P003-02} He finally draws the sword. {same} *Nobody* expected that.
    {P004-01,P004-02} {impact} The blast tears through the hall.

From that, three things are produced:

    recaps/chapter_N.md        what you read: tags removed, editing cues kept
    recaps/chapter_N.tts.txt   what you paste into a TTS tool: plain spoken text
    recaps/chapter_N.cues.json sentence -> panels, cues, emphasised words

The spoken sentences in cues.json are exactly the text in tts.txt, which is
what makes aligning them to the recorded audio reliable.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Dict, List, Set

from ..util import atomic_write_text, require_chapter

TAG = re.compile(r"\{([^{}\n]{1,120})\}")
PANEL_ID = re.compile(r"^P\d{3}-\d{2}$")
CUE_WORDS = {"beat", "impact", "reveal"}
CUE_LINE = re.compile(r"^\s*\[[^\]]*\]\s*$")
INLINE_CUE = re.compile(r"\[[^\]\n]*\]")
SENTENCE_END = re.compile(r"(?<=[.!?…])[\"”’)\]]*\s+")


def strip_tags(text: str) -> str:
    out = TAG.sub("", text or "")
    out = re.sub(r"[ \t]{2,}", " ", out)
    return re.sub(r" +([.,!?;:])", r"\1", out).strip()


def _spoken(text: str) -> str:
    """Narration as it is read aloud: no tags, cues, or markdown."""
    t = TAG.sub("", text)
    t = INLINE_CUE.sub("", t)
    t = re.sub(r"[*_`#>]+", "", t)
    t = re.sub(r"\s+", " ", t)
    return re.sub(r" +([.,!?;:])", r"\1", t).strip()


_NUMBER = re.compile(r"(?<![\w.,$])(\$)?(\d{1,3}(?:,\d{3})+|\d+)(?:\.(\d+))?(st|nd|rd|th|%|s)?(?![\w])")


def spell_numbers(text: str) -> str:
    """Digits as the narrator should say them: 99 -> ninety nine, 100 -> one hundred, 21st ->
    twenty first, 3.5 -> three point five, $50 -> fifty dollars, 1990s -> nineteen nineties.
    Integers of up to 15 digits; anything longer is left alone."""
    from .sync import _ordinal_words, spell_number

    def words(n: int) -> str:
        return " ".join(spell_number(n))

    def repl(m: re.Match) -> str:
        dollar, whole, frac, suffix = m.group(1), m.group(2), m.group(3), m.group(4)
        digits = whole.replace(",", "")
        if len(digits) > 15:
            return m.group(0)
        n = int(digits)
        if suffix in ("st", "nd", "rd", "th") and not frac and not dollar:
            return " ".join(_ordinal_words(n))
        out = words(n)
        if frac:
            out += " point " + " ".join(words(int(d)) for d in frac)
        if dollar:
            out += " dollar" if (n == 1 and not frac) else " dollars"
        elif suffix == "%":
            out += " percent"
        elif suffix == "s":
            head, last = out.rsplit(" ", 1) if " " in out else ("", out)
            plural = (last[:-1] + "ies") if last.endswith("y") else (last + "s")
            out = (head + " " if head else "") + plural
        return out

    return _NUMBER.sub(repl, text)


def tts_text(narration: str, spell: bool = False) -> str:
    """The text to record. With spell, digits are written out as words."""
    paragraphs = []
    for block in re.split(r"\n\s*\n", narration or ""):
        lines = [ln for ln in block.splitlines() if ln.strip() and not CUE_LINE.match(ln)]
        spoken = _spoken(" ".join(lines))
        if spoken:
            paragraphs.append(spell_numbers(spoken) if spell else spoken)
    return "\n\n".join(paragraphs) + "\n"


_ABBREV = ("Mr.", "Mrs.", "Ms.", "Dr.", "No.", "St.", "vs.", "e.g.", "i.e.")
_DOT = "\u0001"

def _protect_abbrev(text: str) -> str:
    for a in _ABBREV:
        text = text.replace(a, a.replace(".", _DOT))
    return text


def _restore_abbrev(text: str) -> str:
    return text.replace(_DOT, ".")


def parse(narration: str, known_panels: Set[str] | None = None, spell: bool = False) -> Dict[str, Any]:
    """Split tagged narration into spoken sentences with their panels and cues.
    With spell, each sentence's text is the spelled-out-numbers text that tts.txt holds."""
    body = "\n".join(ln for ln in (narration or "").splitlines() if not CUE_LINE.match(ln))
    body = re.sub(r"\s+", " ", body).strip()
    body = _protect_abbrev(body)
    sentences: List[Dict[str, Any]] = []
    unknown: List[str] = []
    tagged = 0
    for chunk in SENTENCE_END.split(body):
        chunk = chunk.strip()
        if not chunk:
            continue
        panels, cues = [], []
        for raw in TAG.findall(chunk):
            for tok in re.split(r"[,\s]+", raw.strip()):
                if not tok:
                    continue
                low = tok.lower()
                if low == "same":
                    panels.append("same")
                elif low in CUE_WORDS:
                    cues.append(low)
                elif PANEL_ID.match(tok.upper()):
                    pid = tok.upper()
                    if known_panels is None or pid in known_panels:
                        panels.append(pid)
                    else:
                        unknown.append(pid)
        emphasis = [_spoken(_restore_abbrev(m)) for m in re.findall(r"\*{1,2}([^*\n]{1,60})\*{1,2}", chunk)]
        text = _restore_abbrev(_spoken(chunk))
        if spell:
            emphasis = [spell_numbers(e) for e in emphasis]
            text = spell_numbers(text)
        chunk = _restore_abbrev(chunk)
        if not text:
            # A tag-only fragment: carry its tags onto the next sentence.
            if sentences is not None and (panels or cues):
                sentences.append({"text": "", "panels": panels, "cues": cues, "emphasis": []})
            continue
        # Merge *all* consecutive tag-only fragments, so stacked tags like
        # {P001} {P002} sentence keep every panel instead of dropping the first.
        while sentences and not sentences[-1]["text"]:
            carry = sentences.pop()
            panels = carry["panels"] + panels
            cues = carry["cues"] + cues
        if any(p != "same" for p in panels):
            tagged += 1
        sentences.append({"text": text, "panels": [p for p in panels if p != "same"],
                          "same": "same" in panels and not any(p != "same" for p in panels),
                          "cues": sorted(set(cues)), "emphasis": [e for e in emphasis if e]})
    sentences = [s for s in sentences if s["text"]]
    return {
        "sentences": sentences,
        "tagged_ratio": round(tagged / len(sentences), 3) if sentences else 0.0,
        "unknown_panels": sorted(set(unknown)),
    }


SHORT_SENTENCE_WORDS = 6      # a sentence under this many words is "tiny"
TINY_RUN = 3                  # this many tiny sentences in a row is a run
TAGGED_MIN = 0.90
MIN_HOLD = 3.0                # seconds a picture stays on screen (video.min_hold)
NARRATION_WPM = 150


def _snippet(text: str, n: int = 60) -> str:
    text = text.strip()
    return text if len(text) <= n else text[: n - 1].rstrip() + "\u2026"


def check(narration: str, panels: List[Dict[str, Any]], *, min_hold: float = MIN_HOLD,
          wpm: float = NARRATION_WPM, spell: bool = True) -> Dict[str, List[str]]:
    """What is wrong with a tagged recap before it is saved.

    {"errors": [...], "warnings": [...]}. An error stops the chapter being saved: a tag points at a
    panel the video never shows (lettering-only, a caption, credits), and each message names the
    sentence, the bad panel and the nearest drawn panel to use instead. Warnings are advice that
    does not block: a low tagged share, runs of tiny sentences, digits, too few words for the
    number of drawn panels. Needs the chapter's panels (panels.json rows); without them there is
    nothing to check against.
    """
    errors: List[str] = []
    warnings: List[str] = []
    if not panels:
        return {"errors": errors, "warnings": warnings}
    ids = [p["id"] for p in panels]
    position = {pid: i for i, pid in enumerate(ids)}
    by_id = {p["id"]: p for p in panels}

    def drawn(p: Dict[str, Any]) -> bool:
        return not p.get("text_only") and not p.get("credits")

    drawn_ids = [p["id"] for p in panels if drawn(p)]
    parsed = parse(narration, set(ids))
    sentences = parsed["sentences"]

    def nearest_drawn(pid: str) -> str | None:
        here = position[pid]
        return min(drawn_ids, key=lambda q: (abs(position[q] - here), -position[q]), default=None)

    for n, s in enumerate(sentences, start=1):
        for pid in dict.fromkeys(s.get("panels") or []):
            p = by_id.get(pid)
            if p is None or drawn(p):
                continue
            kind = "credits" if p.get("credits") else "text-only (lettering, a caption or a blank page)"
            near = nearest_drawn(pid)
            hint = f" Tag it {{{near}}} instead (the nearest drawn panel)." if near else ""
            errors.append(f"sentence {n} (\"{_snippet(s['text'])}\") is tagged {pid}, which is {kind} and is never "
                          f"shown in the video.{hint}")

    if sentences:
        ratio = parsed["tagged_ratio"]
        if ratio < TAGGED_MIN:
            warnings.append(f"only {ratio:.0%} of sentences name a panel (aim for 95% or more; below "
                            f"{TAGGED_MIN:.0%} the video falls back on guessing which panel goes where)")
        run: List[int] = []
        for n, s in enumerate(sentences, start=1):
            if len(s["text"].split()) < SHORT_SENTENCE_WORDS:
                run.append(n)
                continue
            if len(run) >= TINY_RUN:
                warnings.append(f"sentences {run[0]}-{run[-1]} are {len(run)} in a row under {SHORT_SENTENCE_WORDS} "
                                "words each; join them into longer sentences so each panel has time to land")
            run = []
        if len(run) >= TINY_RUN:
            warnings.append(f"sentences {run[0]}-{run[-1]} are {len(run)} in a row under {SHORT_SENTENCE_WORDS} "
                            "words each; join them into longer sentences so each panel has time to land")
        text = " ".join(s["text"] for s in sentences)
        digits = [m.group(0) for m in _NUMBER.finditer(text)]      # standalone numbers, not "mp3" or "H2O"
        if digits:
            shown = ", ".join(dict.fromkeys(digits[:4]))
            how = ("the recording text (tts.txt) will have them spelled out; the reading copy keeps digits"
                   if spell else "spell them out (99 -> ninety nine) so the voice and the sync agree")
            warnings.append(f"the script contains digits ({shown}): {how}")
        words = len(text.split())
        need = len(drawn_ids) * float(min_hold) * float(wpm) / 60.0
        if drawn_ids and words < need:
            warnings.append(f"{words} words for {len(drawn_ids)} drawn panels is {words / len(drawn_ids):.1f} per panel; "
                            f"about {need / len(drawn_ids):.1f} are needed ({min_hold:g}s minimum hold at ~{wpm:g} wpm), "
                            f"so the video would have to cram panels. Aim for about {int(need)} words, or tag fewer panels")
    return {"errors": errors, "warnings": warnings}


def has_tags(narration: str) -> bool:
    return any(PANEL_ID.match(t.split(",")[0].strip().upper()) for t in TAG.findall(narration or ""))


def cues_path(root: Path, chapter: str) -> Path:
    return Path(root) / "recaps" / f"chapter_{require_chapter(chapter)}.cues.json"


def tts_path(root: Path, chapter: str) -> Path:
    return Path(root) / "recaps" / f"chapter_{require_chapter(chapter)}.tts.txt"


def write_outputs(root: Path, chapter: str, narration: str, known_panels: Set[str] | None,
                  spell: bool = False) -> Dict[str, Any] | None:
    """Write tts.txt always, and cues.json when the narration carries panel tags."""
    atomic_write_text(tts_path(root, chapter), tts_text(narration, spell))
    cp = cues_path(root, chapter)
    if not has_tags(narration):
        if cp.exists():
            cp.unlink()  # stale cues from an earlier tagged version
        return None
    data = {"version": 1, "chapter": chapter, **parse(narration, known_panels, spell)}
    atomic_write_text(cp, json.dumps(data, indent=1, ensure_ascii=False))
    return data


def load_cues(root: Path, chapter: str) -> Dict[str, Any] | None:
    try:
        data = json.loads(cues_path(root, chapter).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) and data.get("sentences") else None
