# Agent way — copy-paste prompts for another agent

Use these with Antigravity, OpenCode, Cursor, Claude Code, Aider, or any
agent that can run shell commands. One prompt per job. Paste one, let it
finish, then paste the next.

## The one rule for every prompt — do not retype prompt text

The authoritative prompt text lives in exactly one place,
`manhwa_ocr/agent_prompts.py`. The house rule is: never copy prompt text
into other files. So each prompt below tells the agent *which prompt to
read*, and you supply only the series / chapters. Get the full prompt
text from one of these sources, never from memory:

- Dashboard (`python -m manhwa_ocr ui`) — buttons copy ready-made prompts:
  *transcribe*, *recaps*, *build videos*.
- The file itself: `manhwa_ocr/agent_prompts.py` — prompts A–F.
- `USER_GUIDE.md` — section "Which prompt do I use?" lists them verbatim.

Letters: A = transcribe, B = recaps, C = build, D = review, E = intro,
F = fix. Prompt G (Shorts + end-screen + publish checklist) is a
docs-level checklist defined below — it is NOT in `agent_prompts.py`.

## Before you start

1. Open the agent in your **output folder** (`Documents/ManhwaRecaps`), not
   in the program folder. The agent runs the program, never changes it.
2. The agent must use only `python -m manhwa_ocr ...` commands, always with
   `--session "Your Series Name"`, and the venv Python for align/whisper
   steps (system Python lacks pywhispercpp).
3. The agent must never create, convert, trim, or generate audio. If audio
   is missing, it stops and tells you the path.
4. The agent must never rewrite a recap whose chapter already has audio
   unless you ask.
5. If a command fails, the agent stops and shows you the exact command and
   the exact error. It does not work around a tool bug by hand-editing
   generated files.

## Prompt A — Transcribe

```
Use prompt A (transcribe) from manhwa_ocr/agent_prompts.py for the series "Your Series Name".
Read AGENTS.md first. Follow that prompt exactly.
```

What it does: runs `next`, prepares / assembles / finalizes chapters in
order until every chapter is transcribed.

## Prompt B — Recaps

```
Use prompt B (recaps) from manhwa_ocr/agent_prompts.py for the series "Your Series Name", chapters 1-4, about 7 minutes per chapter.
Read AGENTS.md first. Follow that prompt exactly.
```

What it does: `recap --prepare`, writes `parts.json` (original narration,
panel tags, `[HOOK]` / `[LOOP: ...]` markers), `recap --finalize`. Then it
stops and tells you to record each `recaps/chapter_N.tts.txt` as
`audio/chapter_N.mp3`.

## Prompt C — Build videos

```
Use prompt C (build) from manhwa_ocr/agent_prompts.py for the series "Your Series Name", chapters 1-4.
Read AGENTS.md first. Follow that prompt exactly.
```

What it does, per chapter: pair → pair --apply → build → verify →
storyboard review (≤3 rounds) → approve → render. Then it reports
duration, file size, every shot changed + why, every dropped panel +
reason, every pairs.json change.

## Prompt D — Review a storyboard

```
Use prompt D (review) from manhwa_ocr/agent_prompts.py to review the storyboard of chapter N of the series "Your Series Name".
Read AGENTS.md first. Follow that prompt exactly.
```

Use it when you want a second pass on one chapter's storyboard without
rebuilding everything.

## Prompt E — Intro trailer (chapter 0)

```
Use prompt E (intro) from manhwa_ocr/agent_prompts.py for the series "Your Series Name".
Read AGENTS.md first. Follow that prompt exactly.
```

What it does: `video intro --prepare`, you fill `intro/intro.json`, then
it aligns / plans / renders `video/chapter_0.mp4`. You record
`audio/chapter_0.mp3` first.

**Auto hook (no `intro.json` needed):** you write `intro/intro.txt`
(one sentence per line, `[HOOK]` / `[LOOP: ...]` markers), record it as
`audio/chapter_0.mp3` (30s), and the agent runs
`video hook --chapters 1-6 --audio audio/chapter_0.mp3 --script intro/intro.txt`
(or `video intro --auto` with the same flags). It reports the picks from
`intro/intro.auto.json` (sentence → panel + reason) and the hook-structure
warnings. Full rules: `INTRO_HOOK_GUIDE.md`.

**Previously (between parts):** you write
`previously/previously_7.txt`, record it as `audio/previously_7.mp3`
(30s), and the agent runs
`video previously --chapters 1-6 --audio audio/previously_7.mp3 --script previously/previously_7.txt`.
Panels come ONLY from chapters 1–6 (never the next part). Full rules:
`PREVIOUSLY_GUIDE.md`. The agent never generates or trims audio, and
never passes a next-part chapter to `previously`.

## Prompt F — Fix one chapter

```
Use prompt F (fix) from manhwa_ocr/agent_prompts.py to fix chapter N of the series "Your Series Name". The problem is: <describe it in one sentence>.
Read AGENTS.md first. Follow that prompt exactly.
```

The agent fixes only through `pairs.json` / `cues.json` / `fixes.json`,
then re-runs align → plan → verify.

## Prompt G — Shorts + end-screen + publish checklist

Prompt G is a docs-level checklist, not a prompt from `agent_prompts.py`.
Paste it after Prompt C has rendered the chapter video. Start it with the
same common-rules header the agent already has from prompt A (read
AGENTS.md first, `--session` on every command, never touch program code,
never touch audio), then:

```
For chapter N of the series "Your Series Name":
1. Run `python -m manhwa_ocr video shorts --chapter N --session "Your Series Name"` and report the manifest: how many Shorts, each one's title suggestion, hook, and file.
2. Run `python -m manhwa_ocr video endscreen --chapter N --session "Your Series Name"` and report the end-screen window and the WATCH NEXT block.
3. Run `python -m manhwa_ocr video status --session "Your Series Name"` and report the chapter's editor signals: timing_warnings, mask_risk, layout_switches, hook status, chapter_cards, callouts, SFX files present/missing, music file present/missing.
4. If any timing_warnings entry exists, STOP and show me its code + fix. Do not publish a chapter with estimated timing.
5. Tell me exactly what to paste on YouTube: long-video title suggestion, description (chapters + WATCH NEXT block), and one title + hook per Short.
```

## After the agent finishes

- Check its final report against the real files (video exists? duration?
  sync gate passed?).
- You do the human-only steps: record audio, approve, publish, drop
  `music/bed.mp3` / `sfx/*.mp3` files (see `HYBRID_WAY.md`).
