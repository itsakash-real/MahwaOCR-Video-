# Hybrid way — human + agent split

You do the judgment calls and anything that needs your voice, ears, or
taste. The agent does the repetitive vision / alignment / planning work.
Neither side re-does the other's job.

## Split at a glance

| Step | Who | Why |
|---|---|---|
| Paste chapter links into `chapters.txt` | You | Only you know which chapters / links |
| Transcript (prepare → assemble → finalize) | Agent | Vision reading, hundreds of tiles |
| Recap writing (`parts.json` + tags + hook/loops) | Agent | Draft + tagging, from the transcript |
| Recap approval (read it once, fix names/facts) | You | Only you catch a wrong name or invented detail |
| Record narration → `audio/chapter_N.mp3` | You | Your voice. The tool never creates audio |
| Pair / align / plan / verify | Agent | Deterministic + pairing review |
| Storyboard review (every shot vs its sentence) | Agent first pass, you decide | Agent finds wrong pictures; you decide borderline drops/crops |
| Fixes (`fixes.json`) | Agent writes, you approve | You confirm any drop of a panel you care about |
| Approve + render | You approve, agent renders | Approval is a human gate |
| Shorts + end-screen | Agent | Mechanical cut + manifest |
| Music / SFX files | You | You pick + drop royalty-free files into the series folder |
| Publish on YouTube | You | Titles, description, thumbnail, Shorts upload |

## Handoff checkpoints (stop here, do not skip)

1. **Transcript done → you skim.** Agent reports: chapter file exists,
   tile count. You skim the transcript for one wrong character name or
   invented event. If wrong, tell the agent the correction; it fixes the
   transcript, not you.
2. **Recap done → you read it.** Agent reports: tagged ratio, words per
   chapter, hook + loops marked. You read the recap once. Fix facts/names
   only. Do NOT rewrite style — the agent's narration style is the house
   style. If you edit the recap, you must record it again.
3. **Audio recorded → agent resumes.** You say: "audio for chapter N is
   at `audio/chapter_N.mp3`." Agent runs `next` again. It never asks you
   to re-record unless the recap changed.
4. **Storyboard round → you decide.** Agent reports per shot: changed /
   dropped / kept + why. You decide only the shots it flagged "cannot be
   made right" or borderline (e.g. a panel with a visible patch). Say
   "drop it", "keep it", or "crop it" — the agent applies it via
   `fixes.json`.
5. **Render done → you spot-check.** Agent reports: duration, size, sync
   gate, spot-check timestamps. You watch the first 60 seconds + one
   mid-video minute. If you see a grey oval, a wrong picture, or a frozen
   panel, say the timestamp + what you saw; the agent fixes it (Prompt F).
6. **Shorts + end-screen → you publish.** Agent reports: manifest +
   WATCH NEXT block. You paste them on YouTube. You never let the agent
   upload.

## What the agent must never do (even in hybrid mode)

- Create, convert, trim, or generate audio.
- Rewrite a recap whose chapter already has audio unless you ask.
- Hand-edit `plan.json`, `alignment.json` timestamps/panels, or
  `panels.json` to work around a bug. It stops and shows you the exact
  command + error.
- Edit anything under `manhwa_ocr/`, `tests/`, or the program root.
- Use `--force` on render to skip a review or sync gate, or on
  `plan --force` / `build --force` without asking you first.

## Commands you will use most

```bash
python -m manhwa_ocr next --session "Your Series Name"     # what is next
python -m manhwa_ocr video status --session "Your Series Name"  # editor signals
python -m manhwa_ocr video shorts --chapter N --session "Your Series Name"
python -m manhwa_ocr video endscreen --chapter N --session "Your Series Name"
```

Prompts for the agent side are in `AGENT_WAY.md` (A–G).
