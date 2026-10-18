"""Recap-video narration.

Two layers, deliberately separate:

  recap    one narration file per chapter, in <session>/recaps/chapter_N.md.
           Generate any chapter, any time, in any combination. Existing recaps
           are skipped unless forced, so re-running a range only fills gaps.

  compile  stitch any selection of existing per-chapter recaps into one video
           script, e.g. chapters 1-4 and 11, with a runtime table.

The output is *original narration*: the writer's own commentary on what
happens, the way a recap channel actually sounds. It is not a re-reading of the
chapter's dialogue. Short memorable lines get quoted where they land; the rest
is written fresh. That is better craft and the right way to treat someone
else's script.

Each chapter's writer is briefed only with what the series had established by
that chapter, so recapping chapter 1 after transcribing fifty can't leak later
reveals into the narration.

Both layers work in provider mode (the tool calls a model) and agent mode (the
tool writes a task file; the driving agent does the writing).
"""

from __future__ import annotations

import json
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List

from .config import Config
from .memory import SeriesMemory
from .util import (
    UserError,
    atomic_write_json,
    atomic_write_text,
    chapter_key,
    read_agent_json,
    require_chapter,
    sort_chapters,
)
from .workspace import work_root

WORDS_PER_MINUTE = 150
MIN_NARRATION_WORDS = 60
META_RE = re.compile(r"<!--\s*recap-meta\s*(\{.*?\})\s*-->", re.DOTALL)


SCRIPT_SYSTEM = """You write narration for manhwa/manhua recap videos.

Your voice: direct, present tense, conversational. You are telling a friend why \
this chapter is worth caring about, not reading a plot summary aloud.

Hard rules:
- Write ORIGINAL narration in your own words. You are commenting on the chapter, \
not reciting it.
- Quote the source sparingly. A few short, genuinely memorable lines per chapter, \
no more. Never reproduce long stretches of dialogue.
- Never invent events, characters or dialogue that are not in the transcript.
- No filler, no "in this chapter we see", no restating what you just said.

Return only valid JSON. No preamble, no markdown fences."""


SCRIPT_PROMPT = """{briefing_block}{previous_block}{panel_block}
Series: "{series}"
Chapter: {chapter}
Target length: about {words} words (~{minutes} minutes read aloud)

Here is the chapter transcript to work from:

<transcript>
{transcript}
</transcript>

Write the narration segment for this chapter.

Structure it like this:
- Open with a hook -- the single most interesting thing in this chapter, stated \
in a way that makes someone want to keep watching. For chapter 1 of a series, \
hook the premise. Later chapters, hook the escalation.
- Walk the events in order, but as storytelling, not a list. Give the viewer the \
emotional logic: why a character does a thing, why a moment lands.
- Call out the one or two beats that genuinely matter -- a reveal, a reversal, a \
line that recontextualizes something. Say why it matters.
- End on the cliffhanger or the open question that pulls into the next chapter.

Include editing cues in square brackets on their own lines, for example \
`[Panel: the rooftop reveal]` or `[Beat -- hold here]`. These are directions for \
the editor and are not read aloud. Keep them sparse and useful.

Mark emphasis with *asterisks* where the delivery should land.

Stay close to {words} words of spoken narration. Bracketed cues do not count.

Return exactly this shape:
{{
  "narration": "the full narration segment, as markdown",
  "hook": "the opening line, repeated on its own"
}}"""


BOOKENDS_PROMPT = """{briefing_block}
Series: "{series}"

Recap narration exists for chapters {chapter_list}. Write two short pieces to \
top and tail a video covering exactly those chapters.

1. "intro" -- about 80 words. Open the video. Set up the premise without spoiling \
what happens in these chapters. Make someone want to watch.

2. "outro" -- about 60 words. Close the video. Land on where the story stands at \
the end of chapter {last} and what is still unresolved. End on a question or a \
hook, not a summary.

Chapter hooks for reference:

{hooks}

Return exactly this shape:
{{"intro": "...", "outro": "..."}}"""


PANEL_BLOCK = """PANELS
These are this chapter's panels, in reading order.{sheet_note}

{listing}

Tag your narration with the panel that should be on screen: start every sentence \
with the id of the panel it is about, in curly braces, for example \
"{{P003-02}} He finally draws the sword." Use several ids when a sentence covers \
several panels ("{{P004-01,P004-02}} ..."), and "{{same}} " to stay on the current \
panel. You may also add {{beat}} before a dramatic pause, {{impact}} on a hit or \
explosion, and {{reveal}} on a twist. Use only ids from this list, and keep the \
panels roughly in order. Prefer panels with art (faces, action, places): panels \
marked [mostly text] are just speech bubbles or sound effects, and recap videos \
don't show those, since your narration already carries the dialogue. Tags are stripped before the script reaches the \
narrator, so never mention them in the narration itself.

"""


def _panel_listing(panels: List[Dict[str, Any]]) -> str:
    lines = []
    for p in panels:
        if p.get("credits"):
            continue  # scanlator credits: never part of the story
        bits = [p["id"] + (" [tall]" if p.get("tall") else "")
                + (" [TEXT ONLY - don't tag; tag the drawn panel next to it]" if p.get("text_only") else "")]
        if p.get("scene"):
            bits.append(f"shows: {p['scene'][:150]}")
        if p.get("text"):
            bits.append(f"text: {p['text'][:120]}")
        lines.append("- " + " — ".join(bits))
    return "\n".join(lines)


def _target_words(minutes: float) -> int:
    return int(minutes * WORDS_PER_MINUTE)


def _count_words(text: str) -> int:
    """Spoken words only -- bracketed editing cues don't get read aloud."""
    spoken = re.sub(r"\[[^\]]*\]", " ", text or "")
    spoken = re.sub(r"<!--.*?-->", " ", spoken, flags=re.DOTALL)
    return len(spoken.split())


def _spec_name(chapters: List[str]) -> str:
    """['1','2','3','4','11'] -> '1-4_11' for filenames."""
    nums = sorted({int(c) for c in chapters if c.isdigit()})
    others = sort_chapters(c for c in chapters if not c.isdigit())
    runs: List[str] = []
    i = 0
    while i < len(nums):
        j = i
        while j + 1 < len(nums) and nums[j + 1] == nums[j] + 1:
            j += 1
        runs.append(str(nums[i]) if i == j else f"{nums[i]}-{nums[j]}")
        i = j + 1
    parts = runs + [re.sub(r"[^\w.-]", "", o) for o in others]
    return "_".join(p for p in parts if p) or "all"


def _parse_part(entry: Any) -> tuple[str, str]:
    if isinstance(entry, dict):
        return str(entry.get("narration") or ""), str(entry.get("hook") or "")
    if isinstance(entry, str):
        return entry, ""
    return "", ""


class RecapBuilder:
    def __init__(self, config: Config, log=print, session: str | None = None):
        self.cfg = config
        self.log = log
        self.session = session
        self.workdir = config.workdir
        self.recaps_dir = self.workdir / "recaps"
        self.task_dir = work_root(self.workdir) / "recap"
        self.bookends_path = self.recaps_dir / "bookends.json"
        self.memory = SeriesMemory.load(self.workdir / "series_memory.json", config.series_name)

    def _cmd(self, args: str) -> str:
        pin = f' --session "{self.session}"' if self.session else ""
        return f"python -m manhwa_ocr {args}{pin}"

    # ---------------- discovery ----------------

    @staticmethod
    def _labels(folder: Path) -> List[str]:
        if not folder.exists():
            return []
        out = []
        for p in folder.glob("chapter_*.md"):
            label = p.stem[len("chapter_"):]
            if p.stat().st_size > 0:
                try:
                    out.append(require_chapter(label))
                except UserError:
                    continue
        return sort_chapters(out)

    def transcribed(self) -> List[str]:
        return self._labels(self.workdir)

    def recapped(self) -> List[str]:
        return self._labels(self.recaps_dir)

    def recap_path(self, chapter: str) -> Path:
        return self.recaps_dir / f"chapter_{require_chapter(chapter)}.md"

    def pending_task(self) -> List[str] | None:
        """Chapters in a prepared-but-unfinished agent task, else None."""
        plan = self.task_dir / "plan.json"
        if not plan.exists():
            return None
        try:
            chapters = json.loads(plan.read_text(encoding="utf-8")).get("chapters", [])
        except (OSError, ValueError, AttributeError):
            return None
        return [str(c) for c in chapters] or None

    # ---------------- prompt inputs ----------------

    def _transcript(self, chapter: str) -> str:
        p = self.workdir / f"chapter_{require_chapter(chapter)}.md"
        if not p.exists() or p.stat().st_size == 0:
            raise UserError(f"No transcript for chapter {chapter}. Run the OCR pipeline for it first.")
        return p.read_text(encoding="utf-8")

    def _briefing_block(self, upto: str | None = None) -> str:
        briefing = self.memory.as_briefing(upto=upto)
        return f"SERIES CONTEXT\n{briefing}\n\n---\n\n" if briefing.strip() else ""

    def _previous_block(self, chapter: str) -> str:
        """Hand the writer the previous chapter's hook so segments flow."""
        if not chapter.isdigit() or int(chapter) <= 1:
            return ""
        prev = self.recaps_dir / f"chapter_{int(chapter) - 1}.md"
        hook = self._read_meta(prev).get("hook") if prev.exists() else ""
        if not hook:
            return ""
        return (
            f"PREVIOUS SEGMENT\nThe recap for chapter {int(chapter) - 1} opened with: "
            f"\"{hook}\"\nDon't repeat its framing; pick up where it leaves off.\n\n---\n\n"
        )

    def panels(self, chapter: str) -> List[Dict[str, Any]]:
        """This chapter's panels, detecting them if the pages are on disk."""
        from .video.media import list_pages
        from .video.panels import build_panels, load_panels

        data = load_panels(self.workdir, chapter)
        if data is None and list_pages(self.workdir, chapter):
            try:
                data = build_panels(self.workdir, chapter, (self.cfg.video or {}).get("panels"),
                                    log=lambda *_: None)
            except UserError as exc:
                self.log(f"  ! chapter {chapter}: couldn't split panels ({exc}); recap will be untagged")
                return []
        return data["panels"] if data else []

    def _panel_block(self, chapter: str, sheets: List[Path] | None = None) -> str:
        panels = self.panels(chapter)
        if not panels:
            return ""
        note = ""
        if sheets:
            rel = ", ".join(f"`{p.relative_to(self.workdir).as_posix()}`" for p in sheets)
            note = f" Look at the contact sheets ({rel}) to see each panel; ids are printed on them."
        return PANEL_BLOCK.format(sheet_note=note, listing=_panel_listing(panels))

    def _prompt_for(self, chapter: str, minutes: float, *, sheets: List[Path] | None = None) -> str:
        chapter = require_chapter(chapter)
        return SCRIPT_PROMPT.format(
            briefing_block=self._briefing_block(upto=chapter),
            previous_block=self._previous_block(chapter),
            panel_block=self._panel_block(chapter, sheets),
            series=self.cfg.series_name,
            chapter=chapter,
            words=_target_words(minutes),
            minutes=minutes,
            transcript=self._transcript(chapter),
        )

    def _select(self, chapters: List[str], force: bool) -> tuple[List[str], List[str]]:
        """Split a request into (to_write, skipped_existing). Validates input."""
        chapters = sort_chapters(require_chapter(c) for c in chapters)
        have = set(self.transcribed())
        missing = [c for c in chapters if c not in have]
        if missing:
            raise UserError(
                f"No transcript for chapter(s) {', '.join(missing)}. "
                f"Transcribed so far: {', '.join(self.transcribed()) or 'none'}."
            )
        done = set(self.recapped())
        if force:
            return chapters, []
        return [c for c in chapters if c not in done], [c for c in chapters if c in done]

    # ---------------- writing one recap ----------------

    @staticmethod
    def _read_meta(path: Path) -> Dict[str, Any]:
        try:
            m = META_RE.search(path.read_text(encoding="utf-8"))
            return json.loads(m.group(1)) if m else {}
        except (OSError, ValueError):
            return {}

    @staticmethod
    def _body(path: Path) -> str:
        text = path.read_text(encoding="utf-8")
        m = META_RE.search(text)
        return text[m.end():].strip() if m else text.strip()

    def _write_recap(self, chapter: str, narration: str, hook: str, minutes: float) -> Path:
        from .video import cues

        known = {p["id"] for p in self.panels(chapter)} or None
        parsed = cues.write_outputs(self.workdir, chapter, narration, known)
        clean = cues.strip_tags(narration)
        words = _count_words(clean)
        meta = {
            "chapter": chapter,
            "words": words,
            "target_minutes": minutes,
            "hook": cues.strip_tags(hook),
            "tagged": parsed["tagged_ratio"] if parsed else 0.0,
            "written_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }
        if parsed:
            if parsed["unknown_panels"]:
                self.log(f"    ! chapter {chapter}: ignored unknown panel ids {', '.join(parsed['unknown_panels'][:6])}")
            if parsed["tagged_ratio"] < 0.5:
                self.log(f"    ! chapter {chapter}: only {parsed['tagged_ratio']:.0%} of sentences name a panel; "
                         "video sync will be rough. Consider --force to rewrite.")
        out = self.recap_path(chapter)
        atomic_write_text(
            out,
            f"# {self.cfg.series_name} — Chapter {chapter} recap\n\n"
            f"*~{words} words · ~{words / WORDS_PER_MINUTE:.1f} min "
            f"(target {minutes:g} min)*\n\n"
            f"<!-- recap-meta {json.dumps(meta, ensure_ascii=False)} -->\n\n"
            f"{clean}\n",
        )
        return out

    # ---------------- provider mode ----------------

    def generate(self, chapters: List[str], minutes: float, *, force: bool = False) -> List[Path]:
        from .providers import build_provider
        from .util import extract_json

        todo, skipped = self._select(chapters, force)
        if skipped:
            self.log(f"  already recapped, skipping: {', '.join(skipped)} (use --force to redo)")
        if not todo:
            return []

        from .video.cues import strip_tags
        from .video.panels import sheet_paths

        provider = build_provider(self.cfg.provider)
        written = []
        # Sequential on purpose: each recap reads the previous one's hook.
        for chapter in todo:
            self.log(f"  chapter {chapter}...")
            sheets = sheet_paths(self.workdir, chapter)[:12] if self.panels(chapter) else []
            prompt = self._prompt_for(chapter, minutes, sheets=sheets)
            # With panels, the writer needs to see them to tag sentences well.
            raw = provider.vision(SCRIPT_SYSTEM, prompt, sheets) if sheets else provider.text(SCRIPT_SYSTEM, prompt)
            try:
                narration, hook = _parse_part(extract_json(raw))
            except ValueError:
                narration, hook = raw, ""
            if _count_words(strip_tags(narration)) < MIN_NARRATION_WORDS:
                self.log(f"    ! model returned too little narration; chapter {chapter} not saved")
                continue
            path = self._write_recap(chapter, narration, hook, minutes)
            self.log(f"    {_count_words(narration)} words -> {path.name}")
            written.append(path)
        return written

    def generate_bookends(self, chapters: List[str]) -> None:
        from .providers import build_provider
        from .util import extract_json

        chapters = sort_chapters(chapters)
        provider = build_provider(self.cfg.provider)
        hooks = "\n".join(f"- Ch {c}: {self._read_meta(self.recap_path(c)).get('hook', '')}" for c in chapters)
        prompt = BOOKENDS_PROMPT.format(
            briefing_block=self._briefing_block(upto=chapters[-1]),
            series=self.cfg.series_name,
            chapter_list=", ".join(chapters),
            last=chapters[-1],
            hooks=hooks,
        )
        data = extract_json(provider.text(SCRIPT_SYSTEM, prompt))
        if isinstance(data, dict):
            self._write_bookends(chapters, str(data.get("intro") or ""), str(data.get("outro") or ""))

    def _all_bookends(self) -> Dict[str, Dict[str, str]]:
        if not self.bookends_path.exists():
            return {}
        try:
            data = json.loads(self.bookends_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def _write_bookends(self, chapters: List[str], intro: str, outro: str) -> None:
        if not (intro.strip() or outro.strip()):
            return
        allb = self._all_bookends()
        allb[_spec_name(chapters)] = {"intro": intro.strip(), "outro": outro.strip()}
        atomic_write_json(self.bookends_path, allb)

    # ---------------- agent mode ----------------

    def cancel(self) -> bool:
        if not self.task_dir.exists():
            return False
        shutil.rmtree(self.task_dir, ignore_errors=True)
        try:
            self.task_dir.parent.rmdir()  # drop .work/ too if nothing else is in it
        except OSError:
            pass
        return True

    def prepare(self, chapters: List[str], minutes: float, *, force: bool = False) -> Path | None:
        existing = self.pending_task()
        if existing and not force:
            # Re-preparing would throw away narration the agent may already
            # have written into parts.json.
            raise UserError(
                f"A recap task for chapter(s) {', '.join(existing)} is already in progress.\n"
                f"  Finish it:   {self._cmd('recap --finalize')}\n"
                f"  Or discard:  {self._cmd('recap --cancel')}"
            )

        todo, skipped = self._select(chapters, force)
        if skipped:
            self.log(f"  already recapped, skipping: {', '.join(skipped)} (use --force to redo)")
        if not todo:
            return None

        from .video.panels import sheet_paths

        self.cancel()
        prompts_dir = self.task_dir / "prompts"
        prompts_dir.mkdir(parents=True)
        tagged = []
        for chapter in todo:
            sheets = sheet_paths(self.workdir, chapter) if self.panels(chapter) else []
            if sheets:
                tagged.append(chapter)
            atomic_write_text(prompts_dir / f"ch{chapter}.md", self._prompt_for(chapter, minutes, sheets=sheets))

        rel = lambda p: str(p.relative_to(self.workdir)).replace("\\", "/")  # noqa: E731
        listing = "\n".join(
            f"{i:>3}. chapter {c} — `{rel(prompts_dir / f'ch{c}.md')}`" for i, c in enumerate(todo, 1)
        )
        panel_note = ""
        if tagged:
            panel_note = (
                f"\n**Panel tags (chapters {', '.join(tagged)}):** those prompts list the chapter's panels "
                "and name contact-sheet images. Open the images to see what each panel shows, and start "
                "every sentence with the matching `{P003-02}`-style tag as the prompt describes. The video "
                "uses these tags to show the right panel at the right moment.\n"
            )
        doc = f"""# Recap task — {self.cfg.series_name}

Write recap-video narration for {len(todo)} chapter(s): {', '.join(todo)}.
About {_target_words(minutes)} words each (~{minutes:g} minutes read aloud).

---

## System instruction

{SCRIPT_SYSTEM}

---

## Prompts

One file per chapter. Each already contains that chapter's transcript and the
series context as of that chapter. Work through them in order.

{listing}
{panel_note}

---

## Output

Write `{rel(self.task_dir / 'parts.json')}`:

```json
{{
  "{todo[0]}": {{"narration": "...", "hook": "the opening line"}}
}}
```

One key per chapter listed above. If you are interrupted, finalize saves the
chapters that are done and keeps this task open for the rest.

Optionally add `"_intro"` (~80 words) and `"_outro"` (~60 words) for a video
covering exactly chapters {', '.join(todo)}.

## Then

```bash
{self._cmd('recap --finalize')}
```
"""
        atomic_write_json(self.task_dir / "plan.json", {"chapters": todo, "minutes": minutes})
        out = self.task_dir / "TASK.md"
        atomic_write_text(out, doc)
        return out

    def finalize(self) -> List[Path]:
        expected = self.pending_task()
        if not expected:
            raise UserError(f"No recap task in progress. Start one with:  {self._cmd('recap --chapters N --prepare')}")
        plan = json.loads((self.task_dir / "plan.json").read_text(encoding="utf-8"))
        minutes = float(plan.get("minutes", 6))

        raw = read_agent_json(
            self.task_dir / "parts.json",
            f"Write the narration described in {self.task_dir / 'TASK.md'} first.",
        )
        if not isinstance(raw, dict):
            raise UserError('parts.json must be a JSON object like {"1": {"narration": "...", "hook": "..."}}.')
        # Keys may arrive as "01" or 1; normalise them to match the plan.
        parts = {}
        for k, v in raw.items():
            try:
                parts[require_chapter(str(k))] = v
            except UserError:
                parts[str(k)] = v

        from .video.cues import strip_tags

        written, still_owed = [], []
        for chapter in expected:
            narration, hook = _parse_part(parts.get(chapter))
            if _count_words(strip_tags(narration)) < MIN_NARRATION_WORDS:
                still_owed.append(chapter)
                continue
            written.append(self._write_recap(chapter, narration, hook, minutes))

        intro, outro = str(raw.get("_intro") or ""), str(raw.get("_outro") or "")
        if intro or outro:
            self._write_bookends(expected, intro, outro)

        if still_owed:
            self.log(f"  still missing narration for chapter(s) {', '.join(still_owed)}; task kept open")
            atomic_write_json(self.task_dir / "plan.json", {"chapters": still_owed, "minutes": minutes})
        else:
            self.cancel()
        return written

    # ---------------- compile ----------------

    def compile(self, chapters: List[str], *, name: str | None = None) -> Path:
        chapters = sort_chapters(require_chapter(c) for c in chapters)
        if not chapters:
            raise UserError("No chapters selected.")
        missing = [c for c in chapters if not self.recap_path(c).exists()]
        if missing:
            raise UserError(
                f"No recap yet for chapter(s) {', '.join(missing)}. "
                f"Write them first:  recap --chapters {','.join(missing)}"
            )

        spec = _spec_name(chapters)
        allb = self._all_bookends()
        bookends = allb.get(spec, {})
        if not bookends and allb:
            self.log(
                f"  no intro/outro written for chapters {spec.replace('_', ', ')}"
                f" (have them for: {', '.join(k.replace('_', ', ') for k in allb)})"
            )

        rows, segments = [], []
        for c in chapters:
            body = self._body(self.recap_path(c))
            rows.append((c, _count_words(body)))
            segments.append((c, body))

        intro, outro = bookends.get("intro", ""), bookends.get("outro", "")
        total = sum(w for _, w in rows) + _count_words(intro) + _count_words(outro)

        lines = [
            f"# {self.cfg.series_name} — Recap Script (chapters {spec.replace('_', ', ')})",
            "",
            f"*{len(chapters)} chapters · ~{total:,} spoken words · "
            f"~{total / WORDS_PER_MINUTE:.0f} minutes at {WORDS_PER_MINUTE} wpm*",
            "",
            "Bracketed lines are editing cues, not read aloud. *Asterisks* mark emphasis.",
            "",
            "---",
            "",
        ]
        if intro:
            lines += ["## Intro", "", intro, "", "---", ""]
        for c, body in segments:
            lines += [f"## Chapter {c}", "", body, "", "---", ""]
        if outro:
            lines += ["## Outro", "", outro, "", "---", ""]

        lines += ["## Runtime", "", "| Segment | Words | Minutes |", "|---|---|---|"]
        if intro:
            lines.append(f"| Intro | {_count_words(intro)} | {_count_words(intro) / WORDS_PER_MINUTE:.1f} |")
        for c, words in rows:
            lines.append(f"| Chapter {c} | {words:,} | {words / WORDS_PER_MINUTE:.1f} |")
        if outro:
            lines.append(f"| Outro | {_count_words(outro)} | {_count_words(outro) / WORDS_PER_MINUTE:.1f} |")
        lines += [f"| **Total** | **{total:,}** | **{total / WORDS_PER_MINUTE:.1f}** |", ""]

        slug = re.sub(r"[^\w.-]", "-", name).strip("-.") if name else ""
        out = self.recaps_dir / f"compiled_{slug or spec}.md"
        atomic_write_text(out, "\n".join(lines))
        return out
