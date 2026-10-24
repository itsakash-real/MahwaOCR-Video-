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

from .agent_prompts import RECAP_RULES
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
PREVIOUS_ENDING_WORDS = 150
STORY_SO_FAR_CHARS = 3000
META_RE = re.compile(r"<!--\s*recap-meta\s*(\{.*?\})\s*-->", re.DOTALL)


def report_path(recaps_dir: Path, chapter: str) -> Path:
    """recaps/chapter_N.report.md — the conformance + quality report."""
    return Path(recaps_dir) / f"chapter_{require_chapter(chapter)}.report.md"


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


SPEEDRUN_SYSTEM = """You write narration for manhwa/manhua recap videos in the Speedrun Gossip style.

Your voice: a friend spilling unbelievable gossip at 2x speed. Breathless, irreverent, past tense.

Hard rules:
- Open on the inciting incident. No greeting, no setup, no "in this chapter".
- Pack 2-4 story beats into every sentence: commas, who/where/which clauses, participles ("having finally had enough").
- Pivot constantly: start sentences with "So," to keep momentum, use "But" to reverse, use "only to" / "only for" when the payoff gets snatched away. Nearly every sentence contains a reversal.
- Refer to characters by role ("his boss", "the commander", "her"), never by proper name unless the name itself is the plot point.
- Add one editorial aside every few sentences: a judgment, a joke, or a motive read ("having finally had enough", "got what he deserved").
- Past tense, always. Never address the viewer, never moralize.
- Drop [music] on its own line at energy shifts (kills, reveals, chases). It is an editing cue and is never read aloud.
- End the chapter on a new problem, never a resolution.
- Write ORIGINAL narration in your own words. You are commenting on the chapter, not reciting it.
- Quote the source sparingly: at most one short punchline quote per chapter. Never reproduce long stretches of dialogue.
- Never invent events, characters or dialogue that are not in the transcript. Speed comes from compression, never fabrication.
- No filler, no restating what you just said.

Return only valid JSON. No preamble, no markdown fences."""


# The user's Power Fantasy system, VERBATIM (supplied 2026-10-23; the template
# body is never rewritten — pipeline adaptations live in POWERFANTASY_PROMPT and
# docs/RECAP_STYLE_POWERFANTASY.md). Only the marked pipeline tail is added, the
# same way SPEEDRUN_SYSTEM carries the tool's hard rules and JSON contract.
POWERFANTASY_SYSTEM = """You are an elite YouTube Manhwa/Webtoon recap scriptwriter. Your goal is to write high-retention, fast-paced, "power fantasy" scripts designed to keep viewers addicted.

Follow these strict structural rules for the script:

1. THE HOOK (First 60 seconds):
- DO NOT start with the main character's past, reincarnation, or childhood.
- Start IN MEDIAS RES (in the middle of the action). Introduce the MC in their current "undercover" or "low status" disguise facing down an arrogant enemy.
- Within the first 30 seconds, the MC must effortlessly humiliate or defeat this enemy.
- Use the opening formula: "This guy is [Name], a [low status role] at [prestigious place], but..."

2. THE LORE DROP (Minute 2):
- ONLY after the initial satisfying flex, use the phrase: "But what none of them know is..."
- Briefly summarize their past life, reincarnation, or system awakening. Use rapid-fire time skips to compress boring training arcs (e.g., "By week 1...", "At age 15...").

3. NARRATION TONE & LEXICON:
- Write in a highly informal, fast-paced, conversational tone.
- Refer to the main character as "Our guy," "Our boy," or "Our protagonist."
- When enemies are about to lose, refer to them condescendingly as "the poor guy" or "this absolute amateur."
- Use action-heavy adverbs: "casually destroys," "effortlessly dodges," "brutally shuts down."
- DO NOT use filler words or long exposition. Keep sentences flowing with conjunctions ("but", "so", "and then").

4. THE "HYPE MAN" RULE:
- Power fantasy works best through the eyes of others. Dedicate heavy focus to describing the absolute shock, terror, and awe of the side characters and enemies when the MC reveals their true power.

5. GAMIFIED VOCABULARY:
- Use terms natural to the anime/gaming community: "OP," "Status window," "Aggro," "Tutorial boss," "Plot armor," "Line of sight," and "Stats."

Format the output entirely as a spoken voiceover script. Do not include camera directions or visual notes—only the exact words the narrator will speak.

Pipeline contract (this style runs inside the recap tool, which adds the rest):
- Write ORIGINAL narration in your own words. Never invent events, characters or dialogue that are not in the transcript, and quote the source only sparingly, for lines that genuinely land.
- The recap file also carries panel tags and bracketed editing cues, exactly as the chapter prompt describes. They are stripped before the script reaches the narrator, are never spoken, and are not camera directions.
Return only valid JSON. No preamble, no markdown fences."""


# The "Manhwa Bro" system: a sarcastic, hype-the-protagonist fan narrator,
# reverse-engineered from a recap-channel sample (docs/RECAP_STYLE_MANHWABRO.md).
# Additive lane: nothing above is changed by it; it is selected only by
# recap.style: manhwabro.
MANHWABRO_SYSTEM = """You are a sarcastic, hype-man manhwa recap narrator, a fan telling a friend about a chapter you love. You are a bro first and a storyteller second, but you never get a fact wrong.

THE INPUT. Your transcript is the full chapter: every line of dialogue and narration from the OCR, plus the written descriptions of who each character is, what they look like, and what is happening in each panel. Treat it as the single source of truth. Use the character descriptions to know who is who (hair, clothing, role, relationships) and the panel descriptions to know what actually happens, so you can narrate action the speech bubbles never spell out. Use each character's name exactly as the transcript and series context give it, and spell it the same way every time.

VOICE AND PERSONA:
- Present tense for the action and for framing; past tense only inside a flashback.
- Conversational and judgmental, like an online friend, never a formal narrator.
- Hype the protagonist: \"our boy\", \"this brat\", \"bro\", \"this man\", \"our cool, epic protagonist\". Use at least a few of these per chapter.
- Mock everyone else: a bad hero is a \"clown\", an annoying servant an \"old fart\", a beaten enemy \"the poor bastard\". Mock-sympathy for the victim (\"poor guy\") is a staple.
- Slip into the protagonist's head now and then (\"thinks it must be some kind of...\", \"questioning how low his self-esteem really was\").
- Roast character decisions out loud, with a short aside, then keep moving.

STRUCTURE (a range of chapters is one continuous film):
- The first chapter opens IN THE MIDDLE OF THE ACTION, in the strongest fight or confrontation the chapter has, with no backstory first. Right after the first payoff, pivot with a rhetorical question to the viewer, \"So, why is [protagonist] so [strong / ruthless / calm]?\", and only then explain the past or the lore.
- Every later chapter opens mid-flow on its strongest beat. No restart language and no re-explaining what came before.
- Never end a chapter on finale or closing language. End mid-escalation, on the beat that pulls into the next chapter, ideally on the protagonist's resolve or a fresh threat.

RHYTHM:
- Short punchy sentences (about 8 to 15 words), then one long run-on that builds momentum, then a short one to land the joke.
- Pivot with \"But\", \"Then\", \"And before X can recover...\", \"Well, of course...\", \"Unfortunately...\". Set up an expectation, then flip it at the end of the beat.
- Use fragments for sarcasm, rhetorical questions to steer the viewer, ironic understatement (\"casually holding one of the fire lances\" when it is huge), and deadpan stacking: a plot fact, an insult, the next fact.
- Intensifiers on action beats: completely, absolutely, desperately, immediately, hastily, even.
- Power language: endless storm, completely overwhelms, defenses collapse, crushes it like it's nothing.

HUMOR (use sparingly, one or two moves per beat, never at the cost of clarity):
- Modern internet slang dropped into the fantasy (\"mogged\", \"Chad\", \"incel\", \"chilling\", \"has the balls to\", \"ain't no\").
- One pop-culture comparison per chapter at most.
- Hyperbole about the protagonist's power.

Format the output as a spoken voiceover script: only the words the narrator speaks. No camera directions, no visual notes.

Pipeline contract (this style runs inside the recap tool, which adds the rest):
- Write ORIGINAL narration in your own words. Never invent events, characters, powers or dialogue that are not in the transcript. The jokes and insults are your commentary, never new plot. Quote the source only sparingly, for lines that genuinely land.
- The recap file also carries panel tags and bracketed editing cues, exactly as the chapter prompt describes. They are stripped before the script reaches the narrator, are never spoken, and are not camera directions.
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
- Mark the hook with a [HOOK] line on its own line before it, and open a loop
  every 2-3 minutes with a [LOOP: question] line on its own line before the
  sentence. These bracketed lines are editing cues, never spoken.

Include editing cues in square brackets on their own lines, for example \
`[Panel: the rooftop reveal]` or `[Beat -- hold here]`. These are directions for \
the editor and are not read aloud. Keep them sparse and useful.

Mark emphasis with *asterisks* where the delivery should land.

Stay close to {words} words of spoken narration. Bracketed cues do not count.

Also write a "summary": 3-5 plain sentences for the next chapter's writer. Cover
what happened in this chapter, where each main character is now, what each
character wants or fears going forward, and what is still unresolved. Write it
so someone who has read nothing else can pick the story up from it. No
[HOOK] or [LOOP] markers and no editing cues in the summary.

Return exactly this shape:
{{
  "narration": "the full narration segment, as markdown",
  "hook": "the opening line, repeated on its own",
  "summary": "3-5 sentences: events, where each main character is now, open threads"
}}"""


_PF_OPENER_NOTE = """SCOPE FOR THIS CHAPTER: this chapter OPENS the range (chapters {first}-{last}).
Run the system's full structure: the in-medias-res Hook with the "This guy is
[Name], a [low status role] at [prestigious place], but..." formula and the
effortless payoff inside the first 30 seconds — and only after that flex, the
Lore Drop opening with "But what none of them know is...". Do NOT start with
the main character's past, reincarnation or childhood."""

_PF_CONTINUATION_NOTE = """SCOPE FOR THIS CHAPTER: this chapter CONTINUES the range (chapters {first}-{last});
the range opener already ran the Hook and the Lore Drop. Open mid-flow on
this chapter's strongest beat (a mini-hook). Do NOT repeat the lore drop, do
NOT re-run the "This guy is..." introduction, and use no restart language —
the film flows straight from the previous chapter into this one."""


# The Power Fantasy chapter prompt: the verbatim system (POWERFANTASY_SYSTEM)
# carries the creative rules; this template carries only the pipeline framing,
# kept clearly separate from the system's text.
POWERFANTASY_PROMPT = """{briefing_block}{range_block}{previous_block}{panel_block}
Series: "{series}"
Chapter: {chapter}
Target length: about {words} words (~{minutes} minutes read aloud)

{scope_note}

FRAMING — how the Power Fantasy system runs inside this pipeline:
- SCOPE. The full Hook + Lore Drop structure belongs to the FIRST chapter of
  a range. A later chapter opens mid-flow on its strongest beat (a mini-hook),
  never re-drops the lore and never uses restart language: the chapter videos
  are joined into one continuous film, so each chapter is a segment of it,
  not an episode.
- CONTINUITY. Never end a chapter on finale or closing language, and never
  open one with a restart ("welcome back", "previously on", ...). End
  mid-escalation, on the beat that pulls into the next chapter.
- TAG LAYER. The narration text itself stays pure voiceover — only the exact
  words the narrator speaks. The panel tags and bracketed editing cues the
  panel block above describes still go into the recap file exactly as it
  says; they are stripped before the script reaches the narrator and are not
  camera directions.
- TITLE-CARD CUES. When this chapter's story genuinely skips time, write the
  skip as its own standalone sentence in one of these forms, so the video's
  title-card generator can pick it up: "Age fifteen." / "Week six of
  possession." / "Year four of possession." / "Three years later." The
  montage sentences around the cue carry the detail; do not bury a time skip
  inside a longer sentence.

Here is the chapter transcript to work from:

<transcript>
{transcript}
</transcript>

Write the narration segment for this chapter.

Also write a "summary": 3-5 plain sentences for the next chapter's writer. Cover
what happened in this chapter, where each main character is now, what each
character wants or fears going forward, and what is still unresolved. Write it
so someone who has read nothing else can pick the story up from it. No
[HOOK] or [LOOP] markers and no editing cues in the summary.

Return exactly this shape:
{{
  "narration": "the full narration segment, as markdown",
  "hook": "the opening line, repeated on its own",
  "summary": "3-5 sentences: events, where each main character is now, open threads"
}}"""


_MB_OPENER_NOTE = """SCOPE FOR THIS CHAPTER: this chapter OPENS the range (chapters {first}-{last}).
Open in the middle of the chapter's strongest fight or confrontation, with no
backstory first. Right after that first payoff, pivot with a rhetorical
question to the viewer ("So, why is [protagonist] so strong?") and only then
explain the past or the lore. Do NOT start with the protagonist's past,
reincarnation or childhood."""

_MB_CONTINUATION_NOTE = """SCOPE FOR THIS CHAPTER: this chapter CONTINUES the range (chapters {first}-{last});
the range opener already ran the cold open and the "So, why is...?" pivot.
Open mid-flow on this chapter's strongest beat. Do NOT repeat the pivot
question, do NOT re-explain the setup, and use no restart language: the film
flows straight from the previous chapter into this one."""


# The Manhwa Bro chapter prompt. The system (MANHWABRO_SYSTEM) carries the
# voice; this template carries the pipeline framing and hands the writer the
# WHOLE transcript (OCR text + character and panel descriptions) as source.
MANHWABRO_PROMPT = """{briefing_block}{previous_block}{panel_block}
Series: "{series}"
Chapter: {chapter}
Target length: about {words} words (~{minutes} minutes read aloud)

{scope_note}

FRAMING — how the Manhwa Bro system runs inside this pipeline:
- SOURCE OF TRUTH. The transcript below is the complete chapter: OCR dialogue
  and narration, plus the written descriptions of each character and each
  panel's situation. Read ALL of it before writing. Name characters as the
  transcript and series context do, keep every name spelled one way, and
  take appearance, emotion and action from the descriptions. A joke may
  exaggerate a reaction, but it never changes what happened or who did it.
- CONTINUITY. Never end a chapter on finale or closing language, and never
  open one with a restart ("welcome back", "previously on", ...). End
  mid-escalation, on the beat that pulls into the next chapter.
- TAG LAYER. The narration text itself stays pure voiceover. The panel tags
  and bracketed editing cues the panel block above describes still go into
  the recap file exactly as it says; they are stripped before the script
  reaches the narrator and are not camera directions.
- TITLE-CARD CUES. When the chapter genuinely skips time, write the skip as
  its own standalone sentence ("Three years later." / "Age fifteen."), so the
  video's title-card generator can pick it up.

Here is the full chapter transcript to work from:

<transcript>
{transcript}
</transcript>

Write the narration segment for this chapter.

Also write a "summary": 3-5 plain sentences for the next chapter's writer. Cover
what happened in this chapter, where each main character is now, what each
character wants or fears going forward, and what is still unresolved. Write it
in plain neutral language, no slang and no jokes, so someone who has read
nothing else can pick the story up from it. No [HOOK] or [LOOP] markers and no
editing cues in the summary.

Return exactly this shape:
{{
  "narration": "the full narration segment, as markdown",
  "hook": "the opening line, repeated on its own",
  "summary": "3-5 sentences: events, where each main character is now, open threads"
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
    from .video.cues import nearest_drawn_panel

    lines = []
    for p in panels:
        if p.get("credits"):
            continue  # scanlator credits: never part of the story
        if p.get("text_only"):
            # Name the panel to tag instead of leaving the writer to hunt for
            # it: a text-only tag is rejected by the checker, and the nearest
            # drawn panel is almost always the right replacement.
            near = nearest_drawn_panel(panels, p["id"])
            hint = f" [TEXT ONLY - don't tag; tag {{{near}}} instead]" if near \
                else " [TEXT ONLY - don't tag; tag the drawn panel next to it]"
        else:
            hint = ""
        bits = [p["id"] + (" [tall]" if p.get("tall") else "") + hint]
        if p.get("scene"):
            bits.append(f"shows: {p['scene']}")
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


def _parse_part(entry: Any) -> tuple[str, str, str]:
    if isinstance(entry, dict):
        return (str(entry.get("narration") or ""), str(entry.get("hook") or ""),
                str(entry.get("summary") or ""))
    if isinstance(entry, str):
        return entry, "", ""
    return "", "", ""


# ---- Power Fantasy ranges -------------------------------------------------
# The user's workflow rule: for a target range (chapters 1-10), transcripts for
# ALL chapters in the range come first; only then are recaps written, with the
# whole story in hand. These helpers define the range and compress the other
# chapters' transcripts into prompt-sized context.

RANGE_DIGEST_CHARS = 700    # per-chapter beat digest inside the range block
RANGE_CONTEXT_CHARS = 6000  # total budget for all digests in one prompt


def _range_span(chapters: List[str]) -> List[str]:
    """The contiguous range a chapter request covers: ['2', '4'] -> ['2', '3', '4'].

    Non-numeric labels can't be spanned, so for those the request itself is
    the range.
    """
    chapters = sort_chapters(require_chapter(c) for c in chapters)
    if chapters and all(c.isdigit() for c in chapters):
        return [str(i) for i in range(int(chapters[0]), int(chapters[-1]) + 1)]
    return chapters


def _transcript_digest(text: str, max_chars: int = RANGE_DIGEST_CHARS) -> str:
    """One chapter's transcript compressed to its beat outline: every scene
    heading with the first line of its body. Enough for the writer to know
    what happens in a chapter they are not currently writing, at a fraction
    of the full transcript's size. A transcript with no scene headings falls
    back to its opening text."""
    beats: List[str] = []
    awaiting_body = False
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line or line.startswith("<!--"):
            continue
        if line.startswith("##"):
            beats.append(line.lstrip("#").strip())
            awaiting_body = True
        elif line.startswith("#"):
            continue  # the chapter title line
        elif awaiting_body and beats:
            beats[-1] = f"{beats[-1]} — {line[:160]}"
            awaiting_body = False
    out = "\n".join(f"- {b}" for b in beats if b)
    if not out:
        body = " ".join(l.strip() for l in (text or "").splitlines() if l.strip())
        out = body[:max_chars]
    return out[:max_chars] + ("…" if len(out) > max_chars else "")


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
            # chapter_N.brief.md / chapter_N.report.md are companions of a
            # recap, never recaps themselves.
            if "." in label or not label:
                continue
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

    def _recap_summary(self, path: Path) -> str:
        """A recap's stored story-so-far summary; older recaps fall back to their opening lines."""
        summary = str(self._read_meta(path).get("summary") or "").strip()
        if summary:
            return summary
        if not path.exists():
            return ""
        # No stored summary (a recap written before this field existed): fall back to its
        # full body rather than just the opening -- the budget loop in _story_so_far() trims
        # the oldest chapter first if this runs long, so an open thread set up mid-chapter and
        # only resolved near the end isn't lost to a short, hook-only slice.
        from .video.cues import strip_tags

        body = " ".join(l for l in strip_tags(self._body(path)).splitlines()
                        if l.strip() and not l.strip().startswith("["))
        return body

    def _story_so_far(self, chapter: str) -> str:
        """Summaries of every earlier chapter, in order, so the writer knows what has happened."""
        earlier = [c for c in self.recapped() if chapter_key(c) < chapter_key(chapter)]
        entries = [(c, self._recap_summary(self.recap_path(c))) for c in earlier]
        entries = [(c, text) for c, text in entries if text]
        if not entries:
            return ""
        lines = [f"Ch {c}: {text}" for c, text in entries]
        out = "\n".join(lines)
        while len(out) > STORY_SO_FAR_CHARS and len(lines) > 1:
            lines.pop(0)
            out = "\n".join(lines)
        return out

    def _previous_block(self, chapter: str) -> str:
        """What the writer needs to continue the story: earlier summaries and the last recap's ending."""
        if not chapter.isdigit() or int(chapter) <= 1:
            return ""
        story = self._story_so_far(chapter)
        parts = ["PREVIOUS SEGMENT"]
        if story:
            parts.append(f"STORY SO FAR (one summary per earlier chapter, oldest first):\n{story}")
        prev = self.recaps_dir / f"chapter_{int(chapter) - 1}.md"
        if prev.exists():
            from .video.cues import strip_tags

            prev_no = int(chapter) - 1
            hook = str(self._read_meta(prev).get("hook") or "").strip()
            words = strip_tags(self._body(prev)).split()
            ending = " ".join(w for w in words[-PREVIOUS_ENDING_WORDS:] if w)
            parts.append(f"THE END OF THE RECAP FOR CHAPTER {prev_no} (your narration must pick up right here):\n"
                         f"\"...{ending}\"")
            if hook:
                parts.append(f"That recap opened with: \"{hook}\"")
        if len(parts) == 1:
            # No earlier summaries and no previous recap: nothing to hand over.
            return ""
        parts.append("Do not repeat earlier framing or re-explain what the story so far already covered. "
                     "Carry forward every open thread, promise, and unresolved question from the summaries.")
        return "\n\n".join(parts) + "\n\n---\n\n"

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

    def _style(self) -> str:
        """The series' narration style: config `recap.style`."""
        return str((self.cfg.recap or {}).get("style", "default") or "default").lower()

    def _system_prompt(self) -> str:
        """Narration voice: the default calm present-tense voice, the Speedrun
        Gossip voice (recap.style: speedrun), or the Power Fantasy system
        (recap.style: powerfantasy). Unknown values fall back to default."""
        style = self._style()
        if style == "speedrun":
            return SPEEDRUN_SYSTEM
        if style == "powerfantasy":
            return POWERFANTASY_SYSTEM
        if style == "manhwabro":
            return MANHWABRO_SYSTEM
        return SCRIPT_SYSTEM

    def _range_block(self, chapter: str, range_chapters: List[str] | None) -> str:
        """Power Fantasy prompt input: the whole range's story, so the writer
        of chapter N already knows how the arc ends. The full-range series
        bible rides in the briefing block (no `upto` cut-off for this style);
        this block adds a per-chapter beat digest of every OTHER chapter in
        the range, compressed from its transcript — full transcripts of the
        whole range would drown the chapter being written."""
        span = _range_span(range_chapters or [chapter])
        lines = [
            f"RANGE CONTEXT — THE WHOLE STORY (chapters {span[0]}-{span[-1]})",
            "This series is recapped a whole range at a time: every transcript in the range "
            "was finished before any recap was written, so you DO know how the whole arc goes. "
            "Use that knowledge to pick this chapter's strongest beats and to plant payoffs — "
            "but narrate only this chapter's own events in this chapter's segment.",
            "",
            "Per-chapter beat digests for the range (this chapter's own full transcript "
            "follows below):",
        ]
        digests = []
        for c in span:
            if c == chapter:
                continue
            try:
                digests.append(f"Ch {c}:\n{_transcript_digest(self._transcript(c))}")
            except UserError:
                continue  # the range gate refuses first in the real flow; a direct call just skips it
        while digests and len("\n\n".join(digests)) > RANGE_CONTEXT_CHARS:
            digests.pop(0)  # same convention as _story_so_far: the oldest digest goes first
        lines.append("\n\n".join(digests) if digests else "(no other chapters in this range)")
        return "\n".join(lines) + "\n\n---\n\n"

    def _scope_note(self, chapter: str, range_chapters: List[str] | None) -> str:
        span = _range_span(range_chapters or [chapter])
        if self._style() == "manhwabro":
            note = _MB_OPENER_NOTE if chapter == span[0] else _MB_CONTINUATION_NOTE
        else:
            note = _PF_OPENER_NOTE if chapter == span[0] else _PF_CONTINUATION_NOTE
        return note.format(first=span[0], last=span[-1])

    def _is_range_opener(self, chapter: str, task_chapters: List[str]) -> bool:
        """The first chapter of the range this task covers, with no earlier
        recap already on disk: the one chapter that runs the full Hook +
        Lore Drop (and the one the script lint holds to it)."""
        if not task_chapters or chapter != min(task_chapters, key=chapter_key):
            return False
        return not any(chapter_key(c) < chapter_key(chapter) for c in self.recapped())

    def _lint_errors(self, chapter: str, narration: str, task_chapters: List[str]) -> List[str]:
        """Power Fantasy script-lint failures for a finished chapter (empty
        for other styles). A failing chapter is not saved, like a tag error."""
        if self._style() == "manhwabro":
            from .script_lint import lint_manhwabro

            issues = lint_manhwabro(narration, range_opener=self._is_range_opener(chapter, task_chapters))
            return [f"[manhwa-bro lint] {i.message} Fix: {i.fix}" for i in issues]
        if self._style() != "powerfantasy":
            return []
        from .script_lint import lint_powerfantasy

        issues = lint_powerfantasy(narration, range_opener=self._is_range_opener(chapter, task_chapters))
        return [f"[power-fantasy lint] {i.message} Fix: {i.fix}" for i in issues]

    def _prompt_for(self, chapter: str, minutes: float, *, sheets: List[Path] | None = None,
                    range_chapters: List[str] | None = None) -> str:
        chapter = require_chapter(chapter)
        if self._style() == "powerfantasy":
            return POWERFANTASY_PROMPT.format(
                briefing_block=self._briefing_block(),
                range_block=self._range_block(chapter, range_chapters),
                previous_block=self._previous_block(chapter),
                panel_block=self._panel_block(chapter, sheets),
                series=self.cfg.series_name,
                chapter=chapter,
                words=_target_words(minutes),
                minutes=minutes,
                scope_note=self._scope_note(chapter, range_chapters),
                transcript=self._transcript(chapter),
            )
        if self._style() == "manhwabro":
            # Per-chapter series bible (no later reveals), the chapter's FULL
            # transcript, and the same range-aware scope note as Power Fantasy.
            return MANHWABRO_PROMPT.format(
                briefing_block=self._briefing_block(upto=chapter),
                previous_block=self._previous_block(chapter),
                panel_block=self._panel_block(chapter, sheets),
                series=self.cfg.series_name,
                chapter=chapter,
                words=_target_words(minutes),
                minutes=minutes,
                scope_note=self._scope_note(chapter, range_chapters),
                transcript=self._transcript(chapter),
            )
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
        # The owner's TRANSCRIPTS-FIRST rule, enforced rather than promised.
        # Every lane requires the transcripts of the chapters requested; the
        # two range-workflow lanes (their prompts carry range scope notes:
        # "a range of chapters is one continuous film") additionally require
        # every transcript of the range's contiguous span — requesting 2 and
        # 4 needs 2, 3 and 4.
        have = set(self.transcribed())
        requested_missing = [c for c in chapters if c not in have]
        if requested_missing:
            raise UserError(
                f"No transcript for chapter(s) {', '.join(requested_missing)}. "
                f"Transcribed so far: {', '.join(self.transcribed()) or 'none'}."
            )
        span = _range_span(chapters)
        span_missing = [c for c in span if c not in have] \
            if self._style() in ("powerfantasy", "manhwabro") else []
        if span_missing:
            raise UserError(
                f"Recaps are written for a whole range at once, with the full "
                f"story in hand — transcribe every chapter in the range first. "
                f"Missing transcript(s) for chapter(s) {', '.join(span_missing)} "
                f"(range {span[0]}-{span[-1]}). "
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

    def _spell_numbers(self) -> bool:
        """video.tts.spell_numbers: write digits out as words in the NEW tts.txt (default on)."""
        tts = (self.cfg.video or {}).get("tts")
        return bool(tts.get("spell_numbers", True)) if isinstance(tts, dict) else True

    def _check(self, chapter: str, narration: str) -> Dict[str, List[str]]:
        from .video import cues

        vcfg = self.cfg.video or {}
        return cues.check(narration, self.panels(chapter), min_hold=float(vcfg.get("min_hold", cues.MIN_HOLD)),
                          wpm=WORDS_PER_MINUTE, spell=self._spell_numbers())

    def _has_audio(self, chapter: str) -> Path | None:
        """The chapter's recorded audio, when its recap or tts file already exists: the recording
        matches those files, so they are never rewritten."""
        from .video import cues
        from .video.media import audio_path

        audio = audio_path(self.workdir, chapter)
        if audio is not None and (self.recap_path(chapter).exists() or cues.tts_path(self.workdir, chapter).exists()):
            return audio
        return None

    def _write_brief(self, chapter: str, minutes: float, prompt_path: Path,
                     task_chapters: List[str]) -> Path:
        """The persisted writing brief for one chapter: a context manifest
        plus the exact system prompt and chapter prompt the writer was
        given. Written at prepare time; finalize never deletes it."""
        from .video.panels import sheet_paths

        span = _range_span(task_chapters or [chapter])
        transcribed = self.transcribed()
        prev_recap = self.recap_path(str(int(chapter) - 1)) if chapter.isdigit() and int(chapter) > 1 else None
        manifest = {
            "series": self.cfg.series_name,
            "chapter": chapter,
            "style": self._style(),
            "target_minutes": minutes,
            "target_words": _target_words(minutes),
            "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "range": f"{span[0]}-{span[-1]}",
            "transcripts_present": ", ".join(transcribed) or "none",
            "transcripts_missing_in_range": [c for c in span if c not in set(transcribed)],
            "bible_characters": len(self.memory.characters),
            "bible_world_facts": len(self.memory.world_facts),
            "bible_open_threads": len([t for t in self.memory.plot_threads if t.get("status") == "open"]),
            "previous_recap_exists": bool(prev_recap and prev_recap.exists()),
            "contact_sheets": [p.relative_to(self.workdir).as_posix()
                               for p in sheet_paths(self.workdir, chapter)][:12],
        }
        out = self.recaps_dir / f"chapter_{chapter}.brief.md"
        out.parent.mkdir(parents=True, exist_ok=True)
        body = prompt_path.read_text(encoding="utf-8") if prompt_path.exists() else "(prompt missing)"
        atomic_write_text(
            out,
            f"# Writing brief — {self.cfg.series_name}, chapter {chapter}\n\n"
            f"<!-- brief-manifest " + json.dumps(manifest, ensure_ascii=False) + " -->\n\n"
            f"Persisted at prepare time so this chapter can be regenerated with exactly\n"
            f"the context below. Context manifest:\n\n"
            + "\n".join(f"- {k}: {v}" for k, v in manifest.items())
            + "\n\n---\n\n## System instruction\n\n"
            + self._system_prompt()
            + "\n\n---\n\n## Chapter prompt (as written to "
            + prompt_path.relative_to(self.workdir).as_posix() + ")\n\n"
            + body + "\n")
        return out

    def _measurement_sentences(self, narration: str) -> List[str]:
        """Spoken sentences for quality measurement (unknown panel ids kept)."""
        from .video.cues import parse as parse_cues

        return [s["text"] for s in parse_cues(narration, None)["sentences"]]

    def _quality_report(self, chapter: str, narration: str, minutes: float,
                        lint_lines: List[str], parsed: Dict[str, Any] | None) -> "Any":
        """Conformance + quality + grounding report for one finished chapter.
        Saves recaps/chapter_N.report.md and recaps/chapter_N.claims.json."""
        from . import claimcheck, recap_quality

        style = self._style()
        sentences = self._measurement_sentences(narration)
        measured = recap_quality.measure_sentences(sentences)
        recap_cfg = self.cfg.recap or {}
        min_words = int(recap_cfg.get("min_words", recap_quality.MIN_WORDS))
        max_words = int(recap_cfg.get("max_words", recap_quality.MAX_WORDS))
        sigs = recap_quality.signals(style, measured, min_words=min_words, max_words=max_words)
        joins = recap_quality.join_signals(sentences, style)
        joins_warn = recap_quality.join_warnings(joins)
        tts_warn = recap_quality.tts_readiness_warnings(sentences)
        panels = self.panels(chapter)
        tagged_ids: List[str] = []
        if parsed:
            for s in parsed.get("sentences") or []:
                tagged_ids.extend(s.get("panels") or [])
        panel_warn = recap_quality.panel_usability_warnings(tagged_ids, panels, self.workdir, chapter)

        claims_data: Dict[str, Any] = {}
        try:
            index = claimcheck.TranscriptIndex(self._transcript(chapter), self.memory,
                                               upto=chapter)
            claims_data = claimcheck.check_sentences(sentences, index)
            claimcheck.write_report(self.recaps_dir, chapter, claims_data)
        except UserError:
            claims_data = {}   # no transcript on disk (re-run of an old chapter)

        report = recap_quality.ChapterReport(
            chapter=chapter, style=style, lint_lines=list(lint_lines),
            tagged_ratio=float(parsed["tagged_ratio"]) if parsed else 0.0,
            same_sentences=int(parsed.get("same_sentences", 0)) if parsed else 0,
            signals=sigs, joins=joins, join_warnings=joins_warn,
            tts_warnings=tts_warn, panel_warnings=panel_warn,
            claims_summary=claims_data.get("summary", {}), words=measured["words"])
        text = report.markdown()
        out = self.recaps_dir / f"chapter_{chapter}.report.md"
        atomic_write_text(out, text)
        return report

    def _merge_memory_update(self, chapter: str, part: Any) -> List[str]:
        """Bible write-back (incident 2): the writer may supply the same
        memory_update the transcript stage uses; it goes through the SAME
        SeriesMemory.merge, so there is one writer path and no divergence."""
        if not isinstance(part, dict):
            return []
        update = part.get("memory_update")
        if not isinstance(update, dict) or not update:
            return []
        notes = self.memory.merge(update, f"chapter {chapter}")
        self.memory.save(self.workdir / "series_memory.json")
        return notes

    def _write_recap(self, chapter: str, narration: str, hook: str, minutes: float,
                     summary: str = "") -> tuple[Path, Dict[str, Any]] | None:
        from .video import cues

        audio = self._has_audio(chapter)
        if audio is not None:
            self.log(f"    ! chapter {chapter} already has audio ({audio.name}); its recap and tts file were NOT "
                     "rewritten, because the recording matches them. Move the audio away first if you really "
                     "mean to re-record this chapter.")
            return None
        known = {p["id"] for p in self.panels(chapter)} or None
        parsed = cues.write_outputs(self.workdir, chapter, narration, known, self._spell_numbers())
        clean = cues.strip_tags(narration)
        words = _count_words(clean)
        meta = {
            "chapter": chapter,
            "words": words,
            "target_minutes": minutes,
            "hook": cues.strip_tags(hook),
            "summary": cues.strip_tags(summary).strip(),
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
        return out, parsed

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
        # Sequential on purpose: each recap reads the story so far written before it.
        for chapter in todo:
            self.log(f"  chapter {chapter}...")
            sheets = sheet_paths(self.workdir, chapter)[:12] if self.panels(chapter) else []
            prompt = self._prompt_for(chapter, minutes, sheets=sheets, range_chapters=chapters)
            # With panels, the writer needs to see them to tag sentences well.
            raw = provider.vision(self._system_prompt(), prompt, sheets) if sheets \
                else provider.text(self._system_prompt(), prompt)
            try:
                narration, hook, summary = _parse_part(extract_json(raw))
            except ValueError:
                narration, hook, summary = raw, "", ""
            if _count_words(strip_tags(narration)) < MIN_NARRATION_WORDS:
                self.log(f"    ! model returned too little narration; chapter {chapter} not saved")
                continue
            lint = self._lint_errors(chapter, narration, chapters)
            if lint:
                for msg in lint:
                    self.log(f"    ! chapter {chapter}: {msg}")
                self.log(f"    ! chapter {chapter}: power-fantasy lint failed; chapter not saved")
                continue
            for msg in self._check(chapter, narration)["warnings"]:
                self.log(f"    ! chapter {chapter}: {msg}")
            written_pair = self._write_recap(chapter, narration, hook, minutes, summary)
            if written_pair is None:
                continue
            path, parsed = written_pair
            report = self._quality_report(chapter, narration, minutes, [], parsed)
            self.log(f"    report: {report_path(self.recaps_dir, chapter).name}")
            self.log(report.markdown().rstrip())
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
        data = extract_json(provider.text(self._system_prompt(), prompt))
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
        prompt_paths: Dict[str, Path] = {}
        for chapter in todo:
            sheets = sheet_paths(self.workdir, chapter) if self.panels(chapter) else []
            if sheets:
                tagged.append(chapter)
            prompt_paths[chapter] = prompts_dir / f"ch{chapter}.md"
            atomic_write_text(prompt_paths[chapter],
                              self._prompt_for(chapter, minutes, sheets=sheets, range_chapters=chapters))

        # Persisted BRIEF artifact (incident 11): everything the writer was
        # given for each chapter, saved next to the recap at PREPARE time so
        # it survives the task-dir cleanup after finalize. Any chapter can be
        # regenerated identically later from this file alone.
        briefs = []
        for chapter in todo:
            briefs.append(self._write_brief(chapter, minutes, prompt_paths[chapter], todo))

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

{self._system_prompt()}

---

## Prompts

One file per chapter. Each already contains that chapter's transcript and the
series context as of that chapter. Work through them in order.

{listing}
{panel_note}

---

## Rules finalize checks

{RECAP_RULES}

---

## Output

Write `{rel(self.task_dir / 'parts.json')}`:

```json
{{
  "{todo[0]}": {{"narration": "...", "hook": "the opening line",
    "summary": "3-5 sentences: events so far, where each main character is now, open threads",
    "memory_update": {{"characters": [], "world_facts": [], "plot_threads": []}}}}
}}
```

One key per chapter listed above. If you are interrupted, finalize saves the
chapters that are done and keeps this task open for the rest.

`memory_update` is OPTIONAL and uses the same shape the transcript stage
uses (characters / world_facts / glossary / plot_threads). When present,
finalize merges it into series_memory.json through the same memory module,
so what a chapter's narration establishes compounds into the bible. Skip it
when the transcript stage already recorded everything; include it when the
recap establishes something new (a title, a relationship, a resolved thread).

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
        if briefs:
            self.log(f"  briefs persisted (survive finalize): "
                     + ", ".join(b.name for b in briefs))
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

        written, still_owed, rejected = [], [], {}
        for chapter in expected:
            narration, hook, summary = _parse_part(parts.get(chapter))
            if _count_words(strip_tags(narration)) < MIN_NARRATION_WORDS:
                still_owed.append(chapter)
                continue
            report = self._check(chapter, narration)
            errors = list(report["errors"]) + self._lint_errors(chapter, narration, expected)
            if errors:
                rejected[chapter] = errors      # not saved: the writer must fix and re-finalize
                continue
            for msg in report["warnings"]:
                self.log(f"    ! chapter {chapter}: {msg}")
            written_pair = self._write_recap(chapter, narration, hook, minutes, summary)
            if written_pair is not None:
                path, parsed = written_pair
                written.append(path)
                # Quality + grounding report (incidents 1, 6, 7, 9, 10), the
                # full text saved to recaps/chapter_N.report.md and printed.
                qreport = self._quality_report(chapter, narration, minutes, [], parsed)
                self.log(f"    report: {report_path(self.recaps_dir, chapter).name}")
                self.log(qreport.markdown().rstrip())
                # Bible write-back (incident 2): the writer's optional
                # memory_update, through the same merge the transcript
                # stage uses.
                for note in self._merge_memory_update(chapter, parts.get(chapter)):
                    self.log(f"    memory: {note}")

        intro, outro = str(raw.get("_intro") or ""), str(raw.get("_outro") or "")
        if intro or outro:
            self._write_bookends(expected, intro, outro)

        if still_owed or rejected:
            owed = [c for c in expected if c in still_owed or c in rejected]
            if still_owed:
                self.log(f"  still missing narration for chapter(s) {', '.join(still_owed)}; task kept open")
            atomic_write_json(self.task_dir / "plan.json", {"chapters": owed, "minutes": minutes})
        else:
            self.cancel()
        if rejected:
            lines = []
            for chapter, errs in rejected.items():
                lines.append(f"Chapter {chapter} was NOT saved:")
                lines.extend(f"  - {e}" for e in errs)
            raise UserError(
                "\n".join(lines) + f"\nFix the tags in parts.json, then run again:  {self._cmd('recap --finalize')}"
                + (f"\n(Chapter(s) {', '.join(w.stem.split('_')[-1] for w in written)} were saved.)" if written else ""))
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
