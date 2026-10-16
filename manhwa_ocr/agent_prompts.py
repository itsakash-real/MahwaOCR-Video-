"""The one source of truth for every prompt we paste into a coding agent.

Everything that shows an agent prompt is made from this file: the dashboard's "Copy agent
prompt" buttons, `next`, REVIEW.md, the terminal reviewer's checklist, the pairing and recap
task files, and the prompt blocks in USER_GUIDE.md (`python -m manhwa_ocr.agent_prompts` prints
them; tests/test_agent_prompts.py fails when the guide drifts from this file).

Placeholders, written {name}: series (display name), session (the --session value), chapters
(a spec such as 1-7), minutes, N (one chapter number), detail (the error or shot list the
user pastes). Other braces, such as the {same} and {P003-02} recap tags, are left alone.
"""

from __future__ import annotations

import re
from typing import Dict

PLACEHOLDERS = ("series", "session", "chapters", "minutes", "N", "detail")

COMMON_RULES = """Read AGENTS.md first.
Work only on the series "{series}". Put --session "{session}" on every command. Copy commands exactly.
Run only `python -m manhwa_ocr ...` commands.
Never import manhwa_ocr in `python -c`. Never read or patch the program's source.
Never edit anything under manhwa_ocr\\, tests\\ or the program root.
Never create, convert, trim or generate audio. If audio is missing, stop and tell me the path.
Never rewrite a recap whose chapter already has audio unless I ask.
If a command fails or its output is confusing, STOP and show me the exact command and the exact error.
Do not work around a tool bug by hand-editing generated files (alignment.json, plan.json timestamps or panels, panels.json).
Keep a log: append every manual edit you make (file, what, why) to .work\\agent_log.md.
Ask me before running anything with --force that discards edits."""

TRANSCRIBE = """Keep running `python -m manhwa_ocr next --session "{session}"` and do exactly what it says.
Every command it prints already includes --session. Process chapters in order.
Do not stop after one chapter. Stop only when it reports every chapter is transcribed.
If you are interrupted, run the same `next` command again to pick up where you left off."""

RECAP_RULES = """3. Tagging rules. Check each one before you finalize.
   - Tag EVERY sentence. Target a tagged ratio of 95% or higher.
   - Use only drawn panels. Never tag a panel marked text-only, credits or watermark.
   - Put each panel tag on the sentence that is actually spoken over it.
     Never stack several tags on one sentence and leave the next sentence untagged.
   - Use {same} only when the next sentence really continues the same picture.
4. Write for the ear.
   - Spell numbers as words ("ninety-nine", "one hundred").
   - Avoid runs of very short sentences. Keep most sentences at 8 words or more.
   - No stage directions or brackets in spoken text, except the allowed tags.
5. Length check: at least about 7.5 spoken words per drawn panel in the chapter
   (3-second minimum hold at about 150 words per minute).
   Take the panel count from the tool's own output, never from a script of your own.
"""

RECAPS = """1. Run `python -m manhwa_ocr recap --session "{session}" --chapters {chapters} --prepare --minutes {minutes}`.
   Read the TASK.md it prints and open the panel contact sheets it names.
2. Write original narration: commentary, not a re-read of the dialogue.
   Quote the source only briefly, for lines that land.
   Do not add knowledge from later chapters.
""" + RECAP_RULES + """6. Run `python -m manhwa_ocr recap --session "{session}" --finalize`.
   Fix every error. Read every warning. Report the tagged ratio and the word count per chapter.
7. Then STOP. Tell me to record each recaps\\chapter_N.tts.txt and save it as audio\\chapter_N.mp3.
   Remind me: record from the finalized tts.txt. If the recap is edited afterwards, I must record again."""

BUILD = """Do this routine, in order, for chapters {chapters}.
1. `python -m manhwa_ocr video status --session "{session}"`.
   Note anything marked estimated timing, old style or untagged.
2. `python -m manhwa_ocr video pair --chapters {chapters} --session "{session}"`.
   Open every pairing sheet. Fix pairs.json only where a sentence's panel is wrong.
   Then run the same command again with --apply.
3. `python -m manhwa_ocr video build --chapters {chapters} --session "{session}"`.
   If align fails validation, read the exact sentence and reason it prints.
   Fix picture problems in pairs.json, then run `video align --chapter N` and `video plan --chapter N --force`.
   If the failure is about timestamps (a zero-length segment, monotonic), do NOT hand-edit timestamps.
   Stop and report the chapter, the sentence numbers and the times.
4. `python -m manhwa_ocr video verify --chapter N --session "{session}"`.
   Read video\\chapter_N.verify.json, including the low-confidence head list.
5. Open every storyboard sheet and .work\\video\\chN\\NARRATION.md.
   For EVERY shot, compare the picture with the sentence.
   Look for: a wrong picture; text or a watermark on screen; white or blank areas;
   white gutters at card edges; sides cropped off; faces or heads cut;
   patches where bubbles were erased; duplicates; missing important panels.
6. Fix with ONLY these routes.
   - Wrong picture: pairs.json or recaps\\chapter_N.cues.json, then `video align`, then `video plan --force`.
   - Framing (face, crop, drop): video\\chapter_N.fixes.json only. Valid JSON, exact panel ids,
     allowed shapes 9:16, 3:4, 1:1, 4:3, 16:9.
   - Never edit cards[k].panel. Never script-edit plan files.
   - After any re-plan, run `video verify` again so the fixes are re-applied.
7. Repeat verify, storyboard and review until every shot is right (at most 3 rounds).
   If a shot cannot be made right, do not approve. Say which shot and why.
8. `python -m manhwa_ocr video approve --chapter N --session "{session}"`, then
   `python -m manhwa_ocr video render --chapter N --session "{session}"`.
   Do not use --force on render to skip a review or sync gate.
9. Final report per chapter: duration, file size, each shot you changed and why,
   every dropped panel with its reason, every sentence you changed in pairs.json,
   and anything you stopped on."""

# The problems a reviewer looks for. Used by prompt D (REVIEW.md, USER_GUIDE) and by the
# terminal reviewer's checklist (video review), so the two can never disagree.
REVIEW_PROBLEMS = (
    "A WRONG PICTURE: the picture does not show what the sentence describes (wrong character, "
    "wrong place, unrelated art). Note the shot id.",
    "TEXT on screen: speech-bubble dialogue, captions, sound-effect lettering, a logo, a watermark "
    "(for example ASURASCANS.COM), a \"Read at\" credit, or a title card. These must not be shown. "
    "Propose a \"drop\" for a panel that is only text or a logo.",
    "A WHITE or BLANK screen, or an empty wall, door, leaf or floor with no character and no story "
    "value. Propose a \"drop\" for it.",
    "WHITE GUTTERS at the edges of a card: a white or black band along the top, bottom or sides of "
    "the picture. Propose a \"crop\" that stays inside the art.",
    "SIDES CROPPED OFF: a card cut at the left or right so a character, hand or weapon is sliced. "
    "Propose a \"crop\" at full width, if one exists.",
    "A FACE or HEAD cut: half a face, a chin or forehead missing, a character cut at the eyes or "
    "nose. Propose a \"face\" entry with the head's top and bottom as fractions of the panel's "
    "height (0 is the top edge, 1 is the bottom edge), or a \"crop\" entry whose box keeps the "
    "whole head.",
    "A PAINTED PATCH: a flat grey or coloured blob where a bubble was removed. Propose a \"crop\" "
    "that avoids it, if one exists.",
    "An IMPORTANT panel that is missing: a character or story beat the narration describes but no "
    "shot shows. Name the panel ids you can see are missing.",
    "A DUPLICATE in a row, or a clearly wrong order.",
)

FIX_RULES = (
    "Drop only a panel that shows no character and no story detail. Never drop a panel with a "
    "character in it, even a small one.",
    "A \"crop\" must keep one of these shapes: 9:16, 3:4, 1:1, 4:3, 16:9. The box is "
    "[x0, y0, x1, y1] as fractions of the panel (0 to 1), and must keep that shape.",
    "Use the panel id exactly as the storyboard shows it, for example P016-06.",
)

FIXES_EXAMPLE = """{"version": 1,
 "drop": [{"panel": "P016-06", "reason": "sound-effect lettering only"}],
 "face": [{"panel": "P017-02", "top": 0.12, "bottom": 0.46}],
 "crop": [{"panel": "P018-05", "shape": "3:4", "box": [0.0, 0.10, 0.6, 0.85]}],
 "shot": [{"panel": "P019-01", "background": "black", "enter": "fade_in"}]}"""


def _problem_lines() -> str:
    out = []
    for letter, text in zip("abcdefghi", REVIEW_PROBLEMS):
        out.append(f"   {letter}. {text}")
    return "\n".join(out)


def _rule_lines() -> str:
    return "\n".join(f"   - {r}" for r in FIX_RULES)


REVIEW = """You are reviewing the storyboard of a manhwa recap video for chapter {N} of the series "{series}".
You run only `python -m manhwa_ocr ...` commands. Do not edit any program code. Do not delete any file.
Do not hand-edit alignment or plan files. You may only write video/chapter_{N}.fixes.json.
Framing fixes survive re-planning only in fixes.json.

1. Open every storyboard sheet: video/chapter_{N}.storyboard_01.png, _02, _03 (and any more the folder has).
   Open .work/video/ch{N}/NARRATION.md. It lists each shot's time, panel ids and the sentence spoken over it.
   Read it all first.

2. For EVERY shot, compare the picture with the sentence spoken over it.

3. Look for these problems in every shot:
""" + _problem_lines() + """

4. Rules for fixes:
""" + _rule_lines() + """

5. Write your fixes to video/chapter_{N}.fixes.json in this exact form. Keep the file valid JSON.
   Leave a list empty if you have nothing for it. "shot" is optional.

""" + "\n".join("   " + ln for ln in FIXES_EXAMPLE.splitlines()) + """

6. Rebuild and look again:
     python -m manhwa_ocr video verify --chapter {N} --session "{session}"
     python -m manhwa_ocr video storyboard --chapter {N} --session "{session}"
   Then repeat from step 1 with the NEW storyboard images. Stop when every shot is right and no fixes are needed.
   Run at most 3 rounds.

7. When every shot is right, run `python -m manhwa_ocr video approve --chapter {N} --session "{session}"`.
   If a shot cannot be made right, do not approve. Say which shot and why.

8. Your final message must list every shot you changed and why, and every dropped panel with its reason."""

INTRO = """1. Run `python -m manhwa_ocr video intro --prepare --session "{session}"`. Open the contact sheets it writes.
2. Fill intro\\intro.json. Give every sentence 1 to 3 drawn panel references written like "ch2:P010-14".
   Use each panel once. Never use a text-only panel.
3. Build it: `python -m manhwa_ocr video intro --session "{session}"`.
4. Then follow the same verify, review, approve and render steps as the build routine, for chapter 0.
Do not build the intro by hand-copying images, and do not write alignment or plan files yourself."""

FIX = """Fix chapter {N}.
What is wrong (the exact error or the shot list):
{detail}

Fix it with ONLY these routes.
- Wrong picture: pairs.json or recaps\\chapter_{N}.cues.json, then `video align --chapter {N}`, then `video plan --chapter {N} --force`.
- Framing (face, crop, drop): video\\chapter_{N}.fixes.json only. Valid JSON, exact panel ids,
  allowed shapes 9:16, 3:4, 1:1, 4:3, 16:9.
- Never edit cards[k].panel. Never script-edit plan files. Never hand-edit timestamps.
  If the problem is a timestamp, stop and report the chapter, the sentence numbers and the times.
Then run `python -m manhwa_ocr video verify --chapter {N} --session "{session}"`,
`python -m manhwa_ocr video approve --chapter {N} --session "{session}"` and
`python -m manhwa_ocr video render --chapter {N} --session "{session}"`.
Do not use --force on render to skip a review or sync gate."""

BODIES: Dict[str, str] = {
    "transcribe": TRANSCRIBE, "recaps": RECAPS, "build": BUILD, "review": REVIEW, "intro": INTRO, "fix": FIX,
}
LETTERS = {"transcribe": "A", "recaps": "B", "build": "C", "review": "D", "intro": "E", "fix": "F"}

# What the dashboard, USER_GUIDE.md and the tests use. D is a reviewer's brief: it has its own
# rules in its first lines, so the common block is added the same way as to the others.
TEMPLATES: Dict[str, str] = {name: COMMON_RULES + "\n\n" + body for name, body in BODIES.items()}

_PLACEHOLDER = re.compile(r"\{(" + "|".join(PLACEHOLDERS) + r")\}")


def fill(template: str, **values) -> str:
    """Substitute only the known placeholders, so recap tags like {same} survive."""
    return _PLACEHOLDER.sub(lambda m: str(values.get(m.group(1), m.group(0))), template)


def render(name: str, **values) -> str:
    if name not in TEMPLATES:
        raise KeyError(f"unknown prompt {name!r}; known: {', '.join(TEMPLATES)}")
    return fill(TEMPLATES[name], **values)


def review_checklist() -> str:
    """The numbered problem list for the terminal reviewer (video review), same items as prompt D."""
    lines = [f"{i}. {t}" for i, t in enumerate(REVIEW_PROBLEMS, 1)]
    rules = [f"- {r}" for r in FIX_RULES]
    return ("Review every storyboard image below, and the narration list, before you answer.\n\n"
            "For EVERY shot (its label is S### with a time), check:\n" + "\n".join(lines) +
            "\n\nRules for your fixes:\n" + "\n".join(rules) + "\n")


# The values used for the copies in USER_GUIDE.md.
GUIDE_EXAMPLE = {"series": "The Hero Cannot Rest", "session": "the-hero-cannot-rest", "chapters": "1-7",
                 "minutes": "6", "N": "8", "detail": "(paste the exact error, or the shot ids that are wrong)"}
GUIDE_REVIEW_EXAMPLE = {**GUIDE_EXAMPLE, "series": "manhwa-2", "session": "manhwa-2"}


def guide_block(name: str) -> str:
    """The text USER_GUIDE.md must contain for this prompt."""
    values = GUIDE_REVIEW_EXAMPLE if name == "review" else GUIDE_EXAMPLE
    return render(name, **values)


def all_prompts_json() -> Dict[str, str]:
    """Templates for the dashboard (it fills the placeholders itself)."""
    return dict(TEMPLATES)


if __name__ == "__main__":  # python -m manhwa_ocr.agent_prompts [name]
    import sys

    names = sys.argv[1:] or list(TEMPLATES)
    for n in names:
        print(f"===== {LETTERS[n]}: {n} =====\n{guide_block(n)}\n")
