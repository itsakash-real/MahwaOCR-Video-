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
# Per-sentence MOTION overrides for panel-fit mode (B1e): the pairing
# surface (a sentence's "motion" in cues.json, or a pairs.json entry's
# "motion") may overrule the planner's aspect routing for ONE sentence,
# from the user's shot-by-shot rulings — "zoom_out" for a reveal,
# "zoom_out_fast" for a blast, "duo" for a two-panel moment shown side
# by side. A sentence's value is either one of these strings (the whole
# sentence) or a list parallel to its "panels" (one per tagged panel —
# a sliced strip's segments can take different motions: the detonator
# zooms in, the explosion zooms out fast). "duo" is whole-sentence
# only. Unknown values are refused by validate_cues, never ignored.
# B1f adds "zoom_in": the foreground zooms IN at the standard panelfit
# rate (plan.PF_ZOOM_RATE) even on a panel whose aspect would normally
# route it to a drift or a strip traverse — the mirror of "zoom_out".
MOTION_HINTS = ("default", "zoom_in", "zoom_out", "zoom_out_fast", "duo")
CUE_LINE = re.compile(r"^\s*\[[^\]]*\]\s*$")
INLINE_CUE = re.compile(r"\[[^\]\n]*\]")
# P3 editor cues (never spoken; parse() attaches them to the next sentence):
HOOK_MARKER = re.compile(r"^\s*\[HOOK\]\s*$", re.IGNORECASE)
LOOP_MARKER = re.compile(r"\[LOOP:\s*([^\]\n]+)\]", re.IGNORECASE)
HOOK_INLINE = re.compile(r"\[HOOK\]", re.IGNORECASE)
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


# A quoted terminator whose quote closes and is followed by a lowercase
# word is a sub-clause inside a larger sentence ('she asked, "is it over?"
# but no one answered', 'he mutters "fine." and keeps walking'), not the
# end of one -- SENTENCE_END otherwise splits right after the closing
# quote, orphaning the fragment from the tag/panel that opened the
# sentence. Protect the terminator the same way _protect_abbrev protects
# a dot, then restore it once the split is done. A CAPITALISED word after
# the closing quote still splits normally (a genuine new sentence).
_QUOTE_BANG, _QUOTE_Q, _QUOTE_DOT, _QUOTE_ELLIPSIS = "\u0002", "\u0003", "\u0007", "\u0008"
_QUOTE_CONTINUATION = re.compile(r'(["“][^"”\n]{1,200}?)([.!?])(["”])(?=\s+[a-z])')
_QUOTE_MAP = {".": _QUOTE_DOT, "!": _QUOTE_BANG, "?": _QUOTE_Q, "…": _QUOTE_ELLIPSIS}


def _protect_quoted_continuation(text: str) -> str:
    return _QUOTE_CONTINUATION.sub(
        lambda m: m.group(1) + _QUOTE_MAP[m.group(2)] + m.group(3), text)


def _restore_quoted_continuation(text: str) -> str:
    return (text.replace(_QUOTE_BANG, "!").replace(_QUOTE_Q, "?")
            .replace(_QUOTE_DOT, ".").replace(_QUOTE_ELLIPSIS, "…"))


# A terminator INSIDE a quoted span ('he shouts "what is this? is this the
# end?" and collapses') never ends the narrator's sentence: only the span's
# final terminator, at the closing quote, may. Mask every interior one so
# SENTENCE_END cannot split a spoken sentence mid-quote and orphan the rest
# of it from its panel tag. The span's last character is left alone -- the
# continuation rule above decides it.
def _protect_quoted_interior(text: str) -> str:
    def _span(m: re.Match) -> str:
        inner = m.group(2)
        head = inner[:-1] if inner and inner[-1] in ".!?" else inner
        head = re.sub(r"[.!?]", lambda c: _QUOTE_MAP[c.group(0)], head)
        tail = inner[len(head):]
        return m.group(1) + head + tail + m.group(3)

    return re.sub(r'(["“])([^"”\n]*)(["”])', _span, text)


# Bracketed editing cues ("[LOOP: ...?]", "[music]") are never sentence
# boundaries either. SENTENCE_END's trailing class contains "]", so a cue
# that ends in "?]" or ".]" used to split AT the cue's closing bracket,
# decapitating the cue: its literal text leaked into cues.json as an
# untagged pseudo-sentence (later narrated aloud by `video tts`, which
# speaks cues.json), while stealing the tag of the sentence it preceded.
# Mask terminators inside every bracket span so a cue can never split.
_CUE_TERMINATORS = re.compile(r"\[[^\]\n]*\]")


def _protect_bracket_cues(text: str) -> str:
    def _span(m: re.Match) -> str:
        inner = re.sub(r"[.!?…]", lambda c: _QUOTE_MAP[c.group(0)], m.group(0)[1:-1])
        return "[" + inner + "]"

    return _CUE_TERMINATORS.sub(_span, text)


def parse(narration: str, known_panels: Set[str] | None = None, spell: bool = False) -> Dict[str, Any]:
    """Split tagged narration into spoken sentences with their panels and cues.
    With spell, each sentence's text is the spelled-out-numbers text that tts.txt holds.

    P3: whole-line [HOOK] / [LOOP: ...] editing cues are attached to the next
    sentence (as "hook": True / "loops": [...]); inline markers attach to their
    own sentence. They are never spoken (CUE_LINE / INLINE_CUE strip them
    from tts text), and old recaps without markers parse exactly as before.
    """
    # Whole-line markers are editing cues for the NEXT sentence. They are
    # interleaved onto the next non-empty line as zero-width tokens (stripped
    # again in the chunk loop), so a marker can never leak onto an earlier
    # sentence. [Panel: ...] lines still just drop.
    _tokens: List[str] = []
    kept_lines: List[str] = []
    for ln in (narration or "").splitlines():
        if HOOK_MARKER.match(ln):
            _tokens.append("\x00HOOK\x00")
        elif CUE_LINE.match(ln):
            m = LOOP_MARKER.search(ln)
            if m:
                _tokens.append("\x00LOOP:" + m.group(1).strip() + "\x00")
        elif ln.strip():
            kept_lines.append("".join(_tokens) + ln)
            _tokens = []
        else:
            kept_lines.append(ln)
    body = "\n".join(kept_lines)
    body = re.sub(r"\s+", " ", body).strip()
    body = _protect_abbrev(body)
    body = _protect_bracket_cues(body)
    body = _protect_quoted_interior(body)
    body = _protect_quoted_continuation(body)
    sentences: List[Dict[str, Any]] = []
    unknown: List[str] = []
    tagged = 0
    same_sentences = 0
    for chunk in SENTENCE_END.split(body):
        chunk = chunk.strip()
        if not chunk:
            continue
        # P3: markers in this chunk (inline, or tokens interleaved from a
        # preceding whole-line marker) attach to its sentence. Extract and
        # strip the interleaved tokens BEFORE any text is derived, so they
        # can never pollute the spoken sentence.
        chunk_hook = bool(HOOK_INLINE.search(chunk) or "\x00HOOK\x00" in chunk)
        chunk_loops = [_restore_quoted_continuation(m).strip() for m in LOOP_MARKER.findall(chunk)]
        chunk_loops += [_restore_quoted_continuation(m).strip()
                        for m in re.findall(r"\x00LOOP:([^\x00]+)\x00", chunk)]
        chunk = chunk.replace("\x00HOOK\x00", "")
        chunk = re.sub(r"\x00LOOP:[^\x00]+\x00", "", chunk).strip()
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
        emphasis = [_spoken(_restore_quoted_continuation(_restore_abbrev(m)))
                   for m in re.findall(r"\*{1,2}([^*\n]{1,60})\*{1,2}", chunk)]
        text = _restore_quoted_continuation(_restore_abbrev(_spoken(chunk)))
        if spell:
            emphasis = [spell_numbers(e) for e in emphasis]
            text = spell_numbers(text)
        chunk = _restore_quoted_continuation(_restore_abbrev(chunk))
        if not text:
            # A tag-only fragment: carry its tags onto the next sentence.
            if sentences is not None and (panels or cues or chunk_hook or chunk_loops):
                sentences.append({"text": "", "panels": panels, "cues": cues, "emphasis": [],
                                  "hook": chunk_hook, "loops": chunk_loops})
            continue
        # Merge *all* consecutive tag-only fragments, so stacked tags like
        # {P001} {P002} sentence keep every panel instead of dropping the first.
        while sentences and not sentences[-1]["text"]:
            carry = sentences.pop()
            panels = carry["panels"] + panels
            cues = carry["cues"] + cues
            chunk_hook = chunk_hook or bool(carry.get("hook"))
            chunk_loops = list(carry.get("loops") or []) + chunk_loops
        # Honest tagging count: a sentence anchored with {same} IS anchored
        # (it deliberately stays on the current panel), so it counts as
        # tagged. Only sentences with no panel and no {same} are untagged.
        same_only = "same" in panels and not any(p != "same" for p in panels)
        if same_only or any(p != "same" for p in panels):
            tagged += 1
        if same_only:
            same_sentences += 1
        sentence: Dict[str, Any] = {"text": text, "panels": [p for p in panels if p != "same"],
                          "same": "same" in panels and not any(p != "same" for p in panels),
                          "cues": sorted(set(cues)), "emphasis": [e for e in emphasis if e]}
        if chunk_hook:
            sentence["hook"] = True
        if chunk_loops:
            sentence["loops"] = chunk_loops
        sentences.append(sentence)
    sentences = [s for s in sentences if s["text"]]
    return {
        "sentences": sentences,
        "tagged_ratio": round(tagged / len(sentences), 3) if sentences else 0.0,
        "same_sentences": same_sentences,
        "unknown_panels": sorted(set(unknown)),
    }


SHORT_SENTENCE_WORDS = 6      # a sentence under this many words is "tiny"
TINY_RUN = 3                  # this many tiny sentences in a row is a run
TAGGED_MIN = 0.90
TAGGED_HARD_FLOOR = 0.50      # below this the recap is refused, not warned about
MIN_HOLD = 3.0                # seconds a picture stays on screen (video.min_hold)
NARRATION_WPM = 150

# A literal editing cue that survived into a parsed sentence: the bracket
# was never closed (or was decapitated by a split), so its text would be
# spoken aloud by `video tts`, which reads cues.json sentence by sentence.
_CUE_LEAK_RE = re.compile(r"\[(LOOP|HOOK|music|Panel|Beat)", re.IGNORECASE)


def _snippet(text: str, n: int = 60) -> str:
    text = text.strip()
    return text if len(text) <= n else text[: n - 1].rstrip() + "\u2026"


def nearest_drawn_panel(panels: List[Dict[str, Any]], pid: str) -> str | None:
    """The drawn (non-text, non-credits) panel nearest `pid` in story order.

    Ties go to the later panel, matching the hint `check()` has always given.
    """
    position = {p["id"]: i for i, p in enumerate(panels)}
    if pid not in position:
        return None
    drawn_ids = [p["id"] for p in panels if not p.get("text_only") and not p.get("credits")]
    here = position[pid]
    return min(drawn_ids, key=lambda q: (abs(position[q] - here), -position[q]), default=None)


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
    by_id = {p["id"]: p for p in panels}

    def drawn(p: Dict[str, Any]) -> bool:
        return not p.get("text_only") and not p.get("credits")

    drawn_ids = [p["id"] for p in panels if drawn(p)]
    parsed = parse(narration, set(ids))
    sentences = parsed["sentences"]

    for n, s in enumerate(sentences, start=1):
        for pid in dict.fromkeys(s.get("panels") or []):
            p = by_id.get(pid)
            if p is None or drawn(p):
                continue
            kind = "credits" if p.get("credits") else "text-only (lettering, a caption or a blank page)"
            near = nearest_drawn_panel(panels, pid)
            hint = f" Tag it {{{near}}} instead (the nearest drawn panel)." if near else ""
            errors.append(f"sentence {n} (\"{_snippet(s['text'])}\") is tagged {pid}, which is {kind} and is never "
                          f"shown in the video.{hint}")

    # Sentence-integrity guards: an editing cue that leaked into the spoken
    # text, and a fragment a mis-split orphaned from its panel tag. Both are
    # hard errors -- the recording would narrate the garbage.
    for n, s in enumerate(sentences, start=1):
        m = _CUE_LEAK_RE.search(s["text"])
        if m:
            errors.append(
                f"sentence {n} leaks a literal editing cue ({m.group(0)}...): "
                f"\"{_snippet(s['text'])}\". The bracket was probably never closed, or it "
                "was written on the same line as the sentence it precedes. Put "
                "[LOOP: ...] / [HOOK] alone on their own line and close the bracket.")
        first_word = re.match(r"[\s\"“”']*(\w)", s["text"])
        if first_word and first_word.group(1).islower():
            errors.append(
                f"sentence {n} starts lowercase (\"{_snippet(s['text'])}\"): it is a fragment "
                "split off from the sentence (and panel tag) before it. Join it back into "
                "one sentence, or make it a full sentence with its own panel tag.")

    if sentences:
        ratio = parsed["tagged_ratio"]
        if ratio < TAGGED_HARD_FLOOR:
            errors.append(
                f"only {ratio:.0%} of sentences are anchored to a panel (panels or {{same}}); "
                f"below {TAGGED_HARD_FLOOR:.0%} the video cannot follow the narration. "
                "Tag every spoken sentence before finalizing.")
        elif ratio < TAGGED_MIN:
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


def validate_cues(data: Any) -> List[str]:
    """Line-level problems in a cues.json document (P0 safety lock).

    Returns [] when the document is usable. Every problem names the
    sentence index (1-based) and the exact field, so a reviewer can fix
    the file without guessing. This never mutates its input.
    """
    problems: List[str] = []
    if not isinstance(data, dict):
        return ["cues.json must be a JSON object (sentence -> panels, cues, emphasis)"]
    # B1f: per-panel landmark slice hints (optional top-level key; old
    # cues.json files omit it). The format is slicehints.py's docstring;
    # validation is shared with the pairs.json surface so the two can
    # never drift apart.
    if data.get("slice_hints") is not None:
        from .slicehints import parse_slice_hints

        _hints, _problems = parse_slice_hints(data.get("slice_hints"))
        problems.extend(f"cues.json: {p}" for p in _problems)
    sentences = data.get("sentences")
    if not isinstance(sentences, list) or not sentences:
        return ["cues.json: \"sentences\" must be a non-empty list"]
    for i, s in enumerate(sentences, start=1):
        where = f"cues.json sentence {i}"
        if not isinstance(s, dict):
            problems.append(f"{where}: must be an object")
            continue
        if not isinstance(s.get("text"), str) or not s["text"].strip():
            problems.append(f"{where}: \"text\" must be a non-empty string")
        panels = s.get("panels")
        if panels is None:
            problems.append(f"{where}: missing \"panels\" (list of panel ids, may be empty)")
        elif not isinstance(panels, list) or not all(isinstance(p, str) for p in panels):
            problems.append(f"{where}: \"panels\" must be a list of panel-id strings")
        elif len(set(panels)) != len(panels):
            problems.append(f"{where}: duplicate panel ids in \"panels\": {panels}")
        cues = s.get("cues")
        if cues is not None and (not isinstance(cues, list)
                                 or not all(isinstance(c, str) for c in cues)):
            problems.append(f"{where}: \"cues\" must be a list of strings")
        emphasis = s.get("emphasis")
        if emphasis is not None and (not isinstance(emphasis, list)
                                     or not all(isinstance(e, str) for e in emphasis)):
            problems.append(f"{where}: \"emphasis\" must be a list of strings")
        # B1e: the per-sentence motion override (optional; old cues.json
        # files omit it). A string applies to the whole sentence; a list
        # runs parallel to "panels", one entry per tagged panel.
        motion = s.get("motion")
        if motion is not None:
            if isinstance(motion, str):
                if motion not in MOTION_HINTS:
                    problems.append(f"{where}: \"motion\" must be one of "
                                    f"{', '.join(MOTION_HINTS)} (got {motion!r})")
            elif isinstance(motion, list):
                if not all(isinstance(m, str) and m in MOTION_HINTS for m in motion):
                    problems.append(f"{where}: every \"motion\" list entry must be one of "
                                    f"{', '.join(MOTION_HINTS)}")
                elif isinstance(panels, list) and len(motion) != len(panels):
                    problems.append(f"{where}: \"motion\" list has {len(motion)} entries but "
                                    f"\"panels\" has {len(panels)} — they run in parallel")
                elif "duo" in motion:
                    problems.append(f"{where}: \"duo\" is a whole-sentence motion — "
                                    f"write it as a plain string, not in a list")
            else:
                problems.append(f"{where}: \"motion\" must be a string or a list of strings")
        # P3: hook / loop markers (optional; old cues.json files omit them).
        if "hook" in s and not isinstance(s.get("hook"), bool):
            problems.append(f"{where}: \"hook\" must be true or false")
        loops = s.get("loops")
        if loops is not None and (not isinstance(loops, list)
                                  or not all(isinstance(x, str) and x.strip() for x in loops)):
            problems.append(f"{where}: \"loops\" must be a list of non-empty strings")
    return problems


def load_cues(root: Path, chapter: str) -> Dict[str, Any] | None:
    try:
        data = json.loads(cues_path(root, chapter).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) and data.get("sentences") else None


def load_cues_strict(root: Path, chapter: str) -> Dict[str, Any] | None:
    """Like load_cues, but a cues file that exists and is corrupt or invalid
    fails loudly instead of silently looking like "no cues" (which used to
    drop the chapter into the even-timing fallback with the wrong panels).

    Missing file -> None (no cues yet). Corrupt JSON / bad shape ->
    UserError naming the file, the problem, and the fix command.
    """
    from ..util import UserError

    path = cues_path(root, chapter)
    if not path.exists():
        return None
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise UserError(f"{path.name} cannot be read ({exc}). Fix the file, then re-run.") from None
    try:
        data = json.loads(raw)
    except ValueError as exc:
        raise UserError(
            f"{path.name} is not valid JSON ({exc}). Fix the file, then re-run: "
            f"video pair --chapter {chapter}") from None
    problems = validate_cues(data)
    if problems:
        raise UserError(
            f"{path.name} has problems (nothing from it was used):\n  - "
            + "\n  - ".join(problems[:12]))
    return data


def slice_hints_of(cue_data: Dict[str, Any] | None) -> Dict[str, Dict[str, Any]]:
    """The chapter's normalised B1f slice hints from a cues document:
    {panel id: {"slice": [...], "discard": [[a, b], ...]}}. {} when the
    document carries none (the inert case). The document is expected to
    have passed validate_cues (load_cues_strict); a malformed value
    yields {} here rather than raising, because this helper also runs
    on read-only status paths — the strict loader is the gate."""
    if not isinstance(cue_data, dict) or cue_data.get("slice_hints") is None:
        return {}
    from .slicehints import parse_slice_hints

    hints, problems = parse_slice_hints(cue_data.get("slice_hints"))
    return hints if not problems else {}
