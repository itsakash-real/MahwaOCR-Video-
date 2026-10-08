"""Prompt templates.

PROMPT_VERSION is part of the cache key. Bump it whenever you edit a prompt,
otherwise cached results from the old wording get reused and your output stops
being reproducible.
"""

from __future__ import annotations

PROMPT_VERSION = "v1"


# --------------------------------------------------------------------------
# Stage 1: per-tile vision extraction
# --------------------------------------------------------------------------

TILE_SYSTEM = """You transcribe comic pages accurately.

You are given one vertical slice of a webtoon page. Report only what is actually \
visible in the image. Never invent dialogue. If text is cut off at an edge, \
transcribe what you can read and mark it truncated. If text is genuinely \
illegible, say so rather than guessing.

Return only valid JSON. No preamble, no commentary, no markdown fences."""


TILE_PROMPT = """{briefing_block}
This is {label} of "{series}", {chapter_label}.

Transcribe every piece of text in this slice, in top-to-bottom reading order.

For each text element produce one block:
  type     - "dialogue" | "thought" | "narration" | "sfx" | "sign" | "title" | "label"
  speaker  - who is saying it. Use a name from KNOWN CHARACTERS when you can \
identify them by appearance. Use a short descriptor like "silver-haired woman" \
when you cannot. Use null for narration, signs and titles.
  text     - the text exactly as written
  truncated - true if the text runs off the edge of this slice

Also describe what is happening visually in this slice, in one or two sentences, \
under "scene". Mention which characters are present and what they are doing. \
This is used to stitch slices together, so be concrete.

If the slice has no text at all, return an empty blocks array and still fill in \
"scene".

Return exactly this shape:
{{
  "scene": "...",
  "blocks": [
    {{"type": "dialogue", "speaker": "Name or null", "text": "...", "truncated": false}}
  ]
}}"""


# --------------------------------------------------------------------------
# Stage 2: chapter assembly (text-only, cheap)
# --------------------------------------------------------------------------

CHAPTER_SYSTEM = """You assemble raw per-slice comic transcriptions into one clean \
chapter transcript.

You are working from extraction data, not images. Do not add dialogue that is not \
in the input. You may merge blocks that were split across a slice boundary, fix \
obvious reading-order problems, and attribute speakers more confidently using the \
series context. Everything else must survive unchanged.

Return only valid JSON. No preamble, no markdown fences."""


CHAPTER_PROMPT = """{briefing_block}
Series: "{series}" -- {chapter_label}

Below is the raw extraction, slice by slice, in page order. Overlapping slices \
mean some text appears twice; merge duplicates.

{raw_blocks}

Produce two things.

1. "transcript" -- the clean chapter, as markdown. Group it into scenes with \
`## Scene N -- short description` headers. Under each header, put the dialogue \
in order:
   - `**Speaker:** "line"` for dialogue
   - `**Speaker:** *(thinking)* "line"` for thought bubbles
   - `> narration text` for narration boxes
   - `[SFX: ...]` only when the sound effect matters to the story
   - `*(brief visual description)*` where it helps a reader follow the action
   Mark anything uncertain as `[unclear: best guess]`.

2. "memory_update" -- what a reader should carry into the next chapter. Only \
include genuinely new or changed information.
   - characters: for each character who appeared. `appearance` matters most -- \
describe hair, eyes, clothing, anything that identifies them on sight in a later \
chapter. Include `name`, `aliases`, `role`, `speech_style`, `notes`.
   - world_facts: rules, places, organizations, powers established here
   - glossary: in-world terms worth remembering
   - plot_threads: questions this chapter opened or closed. Use \
status "open" or "resolved".

Return exactly this shape:
{{
  "transcript": "## Scene 1 -- ...\\n\\n**Name:** \\"...\\"\\n",
  "summary": "two or three sentences on what happened in this chapter",
  "memory_update": {{
    "characters": [
      {{"name": "...", "aliases": [], "appearance": "...", "role": "...",
       "speech_style": "...", "notes": []}}
    ],
    "world_facts": ["..."],
    "glossary": {{"term": "meaning"}},
    "plot_threads": [{{"thread": "...", "status": "open"}}]
  }}
}}"""


def render_tile_prompt(*, briefing: str, label: str, series: str, chapter_label: str) -> str:
    block = f"SERIES CONTEXT\n{briefing}\n\n---\n\n" if briefing.strip() else ""
    return TILE_PROMPT.format(
        briefing_block=block, label=label, series=series, chapter_label=chapter_label
    )


def render_chapter_prompt(
    *, briefing: str, series: str, chapter_label: str, raw_blocks: str
) -> str:
    block = f"SERIES CONTEXT\n{briefing}\n\n---\n\n" if briefing.strip() else ""
    return CHAPTER_PROMPT.format(
        briefing_block=block,
        series=series,
        chapter_label=chapter_label,
        raw_blocks=raw_blocks,
    )
