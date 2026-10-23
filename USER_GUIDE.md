# User Guide — manhwa chapters to recap videos, with Antigravity

This guide walks the whole job in order: chapter links → transcripts → recap
scripts → your narration → finished videos. You only do three things by hand:
**paste links**, **record audio**, and **paste prompts into your coding agent** (Claude Code or Antigravity).
The agent does the rest.

---

## Which prompt do I use?

Every prompt below is also a button on the dashboard ("Copy agent prompt: ..."). They all start with the same
common rules, and they all come from one file, `manhwa_ocr/agent_prompts.py`, so they never disagree.

| I want to... | Prompt | Where |
|---|---|---|
| Transcribe the chapters | A: transcribe | Step 2 |
| Write recap scripts | B: recaps | Step 3 |
| Make the videos | C: build | Step 5 |
| Check a storyboard shot by shot | D: review | Step 5b, Way A (also in `.work/video/chN/REVIEW.md`) |
| Make the intro (chapter 0) | E: intro | The intro section |
| Fix one chapter after an error or a wrong shot | F: fix chapter | Step 6 |
| Build/continue a wide or adaptive-mode chapter video | I: build workflow (`/build-chapter`, or `docs/CHAPTER_BUILD_WORKFLOW.md`) | Step 5, `docs/AGENT_WAY.md` |

---

## The workflow at a glance

| Step | Who | What happens | What you get |
|---|---|---|---|
| 0 | you, once | Install the tool | — |
| 1 | you | Start a series, paste chapter links | `chapters.txt` |
| 2 | agent | Read every chapter (OCR) | `chapter_N.md` transcripts |
| 3 | agent | Write a recap script per chapter | `recaps/chapter_N.md` + `recaps/chapter_N.tts.txt` |
| 4 | **you** | Record the narration (TTS or your voice) | `audio/chapter_N.mp3` |
| 5 | agent | Build the video, synced to your voice | `video/chapter_N.mp4` |
| 6 | agent / you | Review the storyboard, fix wrong shots | updated video |
| 7 | you | Join chapters, cut Shorts, upload | `video/compiled_1-7.mp4` |

**Lost? Run `python -m manhwa_ocr next`.** It always prints the one thing to do
now, says whether it's your job or the agent's, and gives the exact command. The
dashboard shows the same thing under **Next step**.

---

## Where your files live

The program and your work are kept in two separate folders in Documents:

```
C:\Users\akash\Documents\
├── manhwaOCR\        ← the program (code, settings, this guide). Nothing you make goes here.
└── ManhwaRecaps\     ← everything you make, one folder per series
```

Inside `ManhwaRecaps`, every series gets its own folder, so series never mix:

```
C:\Users\akash\Documents\ManhwaRecaps\the-hero-cannot-rest\
├── chapters.txt            ← STEP 1: you paste chapter links here
├── chapter_1.md            ← STEP 2: transcripts (one per chapter)
├── series_memory.json      ←   character bible, built up chapter by chapter
├── recaps\
│   ├── chapter_1.md        ← STEP 3: the recap script to read
│   ├── chapter_1.tts.txt   ←   the same text, clean, ready for TTS
│   └── compiled_1-7.md     ←   all chapters joined into one script
├── audio\
│   └── chapter_1.mp3       ← STEP 4: YOU put your recording here
├── video\
│   ├── chapter_1.mp4       ← STEP 5: the finished video
│   ├── chapter_1.plan.json ←   the edit (what shows when); rebuilt each time, don't edit
│   ├── chapter_1.fixes.json←   your framing fixes (drop, face, crop, shot); kept across rebuilds
│   └── chapter_1.storyboard_01.png ← one frame per shot, for review
├── media\ch1\              ←   page images + panel sheets (used by video)
└── .work\                  ←   scratch; cleaned automatically, ignore it
```

To find your folder: `python -m manhwa_ocr session info`, or press **Copy folder
path** on the dashboard.

The output location is set by one line in `manhwaOCR\manhwa-ocr.yaml`:
`output_folder: "../ManhwaRecaps"`, which means "next to the program folder".
If you move both folders together, nothing needs changing. To keep your work
somewhere else (another drive, for example), put a full path there, such as
`output_folder: "D:\Manhwa"`, and move the existing series folders across.

---

## Step 0 — One-time setup

Open a terminal in the program folder and install it, with video support:

```bash
cd C:\Users\akash\Documents\manhwaOCR
pip install -e ".[video,sync]"
```

`video` brings FFmpeg (rendering). `sync` brings pywhispercpp (whisper.cpp),
which hears your narration so the pictures change exactly when you talk about
them. The first sync downloads a speech model (tens to a couple hundred MB,
once).

Check it works:

```bash
python -m manhwa_ocr --help
```

Optional, but nice: open the dashboard and leave it open in your browser.

```bash
python -m manhwa_ocr ui
```

It runs at http://127.0.0.1:8765, only on your machine, and refreshes every 5
seconds while the agent works.

---

## Step 1 — Start a series and add chapter links

```bash
python -m manhwa_ocr start "The Hero Cannot Rest"
```

This creates the series folder and makes it the active series. It prints where
`chapters.txt` is. **That file is where your chapter links go.** Open it in
Notepad and paste one link per line, in reading order:

```
# One chapter URL per line, in reading order.

https://asurascans.com/comics/the-hero-cannot-rest-05c7df14/chapter/1
https://asurascans.com/comics/the-hero-cannot-rest-05c7df14/chapter/2
https://asurascans.com/comics/the-hero-cannot-rest-05c7df14/chapter/3
```

Save it. Line 1 is chapter 1, line 2 is chapter 2, and so on. If your links don't
start at chapter 1, number them yourself:

```
47 = https://.../chapter/47
48 = https://.../chapter/48
```

Three other ways to add links, all with the same result:

- Put them straight in the start command:
  `python -m manhwa_ocr start "The Hero Cannot Rest" "https://..." "https://..."`
- Use the command line later: `python -m manhwa_ocr links add "https://..."`
- On the dashboard, use the **Add links** tab.

Duplicates are skipped. Check what it picked up:

```bash
python -m manhwa_ocr status
```

---

## Step 2 — Transcribe the chapters (agent)

Open Antigravity CLI **in your output folder, `C:\Users\akash\Documents\ManhwaRecaps`**
(not the program folder; see "Keeping agents out of the program" below) and paste this prompt (or
press **Copy agent prompt: transcribe** on the dashboard, which fills in the
series for you):

```text
Read AGENTS.md first.
Work only on the series "The Hero Cannot Rest". Put --session "the-hero-cannot-rest" on every command. Copy commands exactly.
Run only `python -m manhwa_ocr ...` commands.
Never import manhwa_ocr in `python -c`. Never read or patch the program's source.
Never edit anything under manhwa_ocr\, tests\ or the program root.
Never create, convert, trim or generate audio. If audio is missing, stop and tell me the path.
Never rewrite a recap whose chapter already has audio unless I ask.
If a command fails or its output is confusing, STOP and show me the exact command and the exact error.
Do not work around a tool bug by hand-editing generated files (alignment.json, plan.json timestamps or panels, panels.json).
Keep a log: append every manual edit you make (file, what, why) to .work\agent_log.md.
Ask me before running anything with --force that discards edits.

Keep running `python -m manhwa_ocr next --session "the-hero-cannot-rest"` and do exactly what it says.
Every command it prints already includes --session. Process chapters in order.
Do not stop after one chapter. Stop only when it reports every chapter is transcribed.
If you are interrupted, run the same `next` command again to pick up where you left off.
```

For each chapter the agent downloads the pages, cuts the tall strips into
readable pieces, reads them, and writes `chapter_N.md`. The character memory
grows as it goes, so chapter 6 still knows who everyone is.

No API key is needed. Antigravity's own model does the reading.

**If the agent stops partway,** paste the same prompt again. `next` knows exactly
where it got to, and nothing is lost.

**If a site blocks downloading,** `next` will say so. Save the chapter's images
with your browser into a folder, then tell the agent:

```bash
python -m manhwa_ocr prepare --chapter 5 --folder "C:\path\to\saved-images"
```

(Have an API key instead? `python -m manhwa_ocr run --all-pending` does step 2 on
its own, without an agent.)

---

## Step 3 — Write the recap scripts (agent)

When every chapter is transcribed, `next` moves on to recaps. Paste this into
Antigravity (or press **Copy agent prompt: recaps**):

```text
Read AGENTS.md first.
Work only on the series "The Hero Cannot Rest". Put --session "the-hero-cannot-rest" on every command. Copy commands exactly.
Run only `python -m manhwa_ocr ...` commands.
Never import manhwa_ocr in `python -c`. Never read or patch the program's source.
Never edit anything under manhwa_ocr\, tests\ or the program root.
Never create, convert, trim or generate audio. If audio is missing, stop and tell me the path.
Never rewrite a recap whose chapter already has audio unless I ask.
If a command fails or its output is confusing, STOP and show me the exact command and the exact error.
Do not work around a tool bug by hand-editing generated files (alignment.json, plan.json timestamps or panels, panels.json).
Keep a log: append every manual edit you make (file, what, why) to .work\agent_log.md.
Ask me before running anything with --force that discards edits.

1. Run `python -m manhwa_ocr recap --session "the-hero-cannot-rest" --chapters 1-7 --prepare --minutes 6`.
   Read the TASK.md it prints and open the panel contact sheets it names.
2. Write original narration: commentary, not a re-read of the dialogue.
   Quote the source only briefly, for lines that land.
   Do not add knowledge from later chapters.
   Narration voice: the default calm present-tense voice, unless the series
   config sets `recap.style: speedrun` — then use the Speedrun Gossip voice
   (docs/RECAP_STYLE_SPEEDRUN.md): past tense, role labels instead of names,
   chain-of-consequences sentences with So/But/only-to pivots, an editorial
   aside every few sentences, open on the inciting incident, end mid-escalation.
   Or `recap.style: powerfantasy` — then use the Power Fantasy system
   (docs/RECAP_STYLE_POWERFANTASY.md): a range is recapped only after every
   transcript in it exists; the range opener hooks in medias res with the
   "This guy is..." formula and drops the lore after the flex; later chapters
   open mid-flow; no chapter ends on finale language or opens with a restart.
   Or `recap.style: manhwabro` — then use the Manhwa Bro voice
   (docs/RECAP_STYLE_MANHWABRO.md): cold open in the fight, a "So, why is he so
   strong?" pivot into the lore, "our boy / this brat / bro" for the protagonist,
   roasts for everyone else; write from the whole transcript, descriptions included.
3. Tagging rules. Check each one before you finalize.
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
-6. Hook and open loops (P3 editor layer).
   - Open with a hook: the first 30 seconds must give proof (the exact
     rank/number/beat the title promises), stakes, and a one-line roadmap.
     Mark the hook sentence with [HOOK] on its own line before it.
   - Open a loop every 2-3 minutes: a question the viewer must keep watching
     to answer. Mark it [LOOP: the question] on its own line before the sentence.
   - End on an open question or cliffhanger, not a summary.
   - [HOOK] / [LOOP: ...] are editing cues, never spoken: they never appear
     in tts.txt, and the bracketed text is not read aloud.
7. Run `python -m manhwa_ocr recap --session "the-hero-cannot-rest" --finalize`.
   Fix every error. Read every warning. Report the tagged ratio and the word count per chapter.
8. Then STOP. Tell me to record each recaps\chapter_N.tts.txt and save it as audio\chapter_N.mp3.
   Remind me: record from the finalized tts.txt. If the recap is edited afterwards, I must record again.
```

- `--chapters` takes `3`, `1-7`, or `1-4,11`. Chapters that already have a recap
  are skipped, so asking for `1-10` after doing `1-4` only writes 5–10. Add
  `--force` to rewrite.
- `--minutes` sets the length per chapter: `5` ≈ 750 words, `6` ≈ 900, `8` ≈ 1,200.
- On the dashboard you can tick any chapters and press **Copy recap command**.

Each chapter gets two files in `recaps/`:

- `chapter_N.md` is the script to read and check.
- `chapter_N.tts.txt` is the same narration as plain text, ready for your TTS
  tool. **This is the file you record from.**

While writing, the agent also tags each sentence with the panel it talks about.
You never see the tags; they are how the video knows which picture to show.

The narration is **original commentary**: the story told in your voice, with only
short quotes where a line really lands. That's what recap channels sound like,
and it keeps the video your own work.

---

## Step 4 — Record the narration (you)

This is the one step the agent can't do. Once recaps are written, `next` says
something like:

```
STEP 3 (you, not the agent) - record narration for chapter(s) 1-7:
  Script to read or paste into TTS:  ...\recaps\chapter_1.tts.txt
  Save the recording as:            ...\audio\chapter_1.mp3
```

For each chapter:

1. **Get the script.** On the dashboard's **Video** tab, press **Script** for the
   chapter, then **Copy**. Or open `recaps\chapter_N.tts.txt` directly.
2. **Record it.** Paste it into your TTS tool, or read it yourself. Record the
   whole script as written. Small slips are fine, because the sync listens to
   what was actually said.
3. **Put the audio in place.** Either:
   - press **Upload** on the Video tab, next to the chapter, or
   - save it as `audio\chapter_N.mp3` in the series folder. `.wav` and `.m4a`
     work too, and `chapter_01.mp3` is fine.

One audio file per chapter. Uploading again replaces the old one, and the video
is marked as needing a rebuild.

### Or generate the narration with Deepgram TTS

Instead of recording, you can synthesize the narration from the finalized
recap with one command per chapter:

```
python -m manhwa_ocr video tts --chapter N --session "<name>"
```

Before speaking anything, it lints every sentence for the specific patterns
that cause a bad Aura read -- raw digits, a dash/ellipsis standing in for a
sentence break, stacked punctuation, a missing terminal mark, unbalanced
quotes, a run-on sentence over ~30 words -- and refuses the whole chapter if
anything's flagged, with the sentence number and a suggested fix for each.
Fix the recap and re-finalize, then re-run.

Once synthesis is clean, it speaks each sentence of
`recaps\chapter_N.cues.json` with a Deepgram Aura voice, joins them with a
short pause, and writes `audio\chapter_N.m4a` (plus a `chapter_N.tts.json`
manifest with the voice used and per-sentence timings). It then
automatically re-transcribes the result with the same whisper engine
`video align` uses and flags any sentence whose heard words don't match the
script, or whose timing looks like truncated/corrupt audio -- written to
`chapter_N.tts_verify.json`, so you know which specific sentence (if any)
is worth listening to instead of the whole file. Skip this check with
`--no-verify`, or re-run it alone with `video tts-verify --chapter N`.
From there the usual flow applies unchanged: `video pair`, `video align`,
`video plan`, and so on. Because the audio speaks the script exactly as
written, align has no rewordings to trip over.

The Deepgram API key is read from the `DEEPGRAM_API_KEY` environment variable,
or from a key file (default `~/.config/manhwa/deepgram.key`, one line, the key
only). Keep the key on your own machine: it is never written into the series
folder or the repo. Pick the voice with `--voice aura-2-orion-en` or a `tts:`
section in the config (`model`, `gap_seconds`, `key_file`, `speed`).
Playback speed defaults to `1.23` (Aura-2 models only, range 0.7-1.5) --
a touch faster reads less like a performance, more like a narrator moving
the story along; override per command with `--speed 1.1`. Re-running the
command reuses cached sentences; `--force` re-synthesizes everything.

**Important:** record from the *current* script. If a recap is rewritten later,
record it again, or the pictures won't line up with your words.

### Before you record audio: the checklist

The pictures are matched to the script as it stands now, so settle the script
first. If the agent says a chapter is ready, check these (the tool warns about
most of them when the recap is finalized):

- [ ] **At least 95% of the sentences carry a panel tag.** Under 90% is a warning.
- [ ] **One panel per sentence** wherever possible (a few sentences may show two).
- [ ] **Drawn panels only.** No tag on a caption, a blank page or a credits panel;
      finalize refuses those and names the sentence.
- [ ] **Numbers are spelled out** in `chapter_N.tts.txt` (new recaps do this
      automatically: 99 becomes "ninety nine"), so the voice and the text agree.
- [ ] **No run of three or more very short sentences** (under six words each);
      they flash by too quickly to show a picture.
- [ ] **Enough words for every panel**: about 7.5 words per drawn panel, so each
      picture can stay up for its minimum time.
- [ ] **Never record from a script that was edited after finalizing.** If the
      recap text changes at all, have the agent run `recap --finalize` again and
      record the new `chapter_N.tts.txt`. Never re-record from a hand-edited script.

---

## Step 5 — Build the videos (agent)

As soon as a chapter has audio, `next` says to build its video. Paste this into
Antigravity (or press **Copy agent prompt: build videos**, which includes every
chapter that has audio):

```text
Read AGENTS.md first.
Work only on the series "The Hero Cannot Rest". Put --session "the-hero-cannot-rest" on every command. Copy commands exactly.
Run only `python -m manhwa_ocr ...` commands.
Never import manhwa_ocr in `python -c`. Never read or patch the program's source.
Never edit anything under manhwa_ocr\, tests\ or the program root.
Never create, convert, trim or generate audio. If audio is missing, stop and tell me the path.
Never rewrite a recap whose chapter already has audio unless I ask.
If a command fails or its output is confusing, STOP and show me the exact command and the exact error.
Do not work around a tool bug by hand-editing generated files (alignment.json, plan.json timestamps or panels, panels.json).
Keep a log: append every manual edit you make (file, what, why) to .work\agent_log.md.
Ask me before running anything with --force that discards edits.

Do this routine, in order, for chapters 1-7.
1. `python -m manhwa_ocr video status --session "the-hero-cannot-rest"`.
   Note anything marked estimated timing, old style or untagged.
2. `python -m manhwa_ocr video pair --chapters 1-7 --session "the-hero-cannot-rest"`.
   Open every pairing sheet. Fix pairs.json only where a sentence's panel is wrong.
   Then run the same command again with --apply.
3. `python -m manhwa_ocr video build --chapters 1-7 --session "the-hero-cannot-rest"`.
   If align fails validation, read the exact sentence and reason it prints.
   Fix picture problems in pairs.json, then run `video align --chapter N` and `video plan --chapter N --force`.
   If the failure is about timestamps (a zero-length segment, monotonic), do NOT hand-edit timestamps.
   Stop and report the chapter, the sentence numbers and the times.
4. `python -m manhwa_ocr video verify --chapter N --session "the-hero-cannot-rest"`.
   Read video\chapter_N.verify.json, including the low-confidence head list.
5. Open every storyboard sheet and .work\video\chN\NARRATION.md.
   For EVERY shot, compare the picture with the sentence.
   Look for: a wrong picture; text or a watermark on screen; white or blank areas;
   white gutters at card edges; sides cropped off; faces or heads cut;
   patches where bubbles were erased; duplicates; missing important panels.
6. Fix with ONLY these routes.
   - Wrong picture: pairs.json or recaps\chapter_N.cues.json, then `video align`, then `video plan --force`.
   - Framing (face, crop, drop): video\chapter_N.fixes.json only. Valid JSON, exact panel ids,
     allowed shapes 9:16, 3:4, 1:1, 4:3, 16:9.
   - Never edit cards[k].panel. Never script-edit plan files.
   - After any re-plan, run `video verify` again so the fixes are re-applied.
7. Repeat verify, storyboard and review until every shot is right (at most 3 rounds).
   If a shot cannot be made right, do not approve. Say which shot and why.
8. `python -m manhwa_ocr video approve --chapter N --session "the-hero-cannot-rest"`, then
   `python -m manhwa_ocr video render --chapter N --session "the-hero-cannot-rest"`.
   Do not use --force on render to skip a review or sync gate.
9. Final report per chapter: duration, file size, each shot you changed and why,
   every dropped panel with its reason, every sentence you changed in pairs.json,
   and anything you stopped on.
```

You can also run the build yourself without an agent:

```bash
python -m manhwa_ocr video build --chapters 1-7
```

What `video build` does, per chapter:

1. **Panels:** splits the pages into individual panels.
2. **Align:** listens to your audio to find when each sentence starts, then
   deterministically decides which panel shows for every sentence (or, for a
   sentence that's just a thought or a sound with no visible actor, holds
   whatever panel is already on screen rather than guessing one). Writes
   `chapter_N.alignment.json` and validates its own output; this is the only
   step that decides *which picture*, and the only step whose result you may
   need to glance over (it prints a short review queue when something's
   genuinely ambiguous).
3. **Plan:** turns the alignment's decisions into shots — crop, layout,
   entrance, background. It never re-decides which panel to show.
4. **Storyboard:** one frame per shot, so you can check it at a glance.
5. **Render:** the final 1080p MP4.

Steps that are already done are skipped, so running it again is cheap. It takes
about as long as the video itself (a 6-minute chapter takes about 6 minutes).

**Want a quick look first?** Add `--preview` for a fast 480p version.

### What the video looks like

The editing follows the usual recap channel style:

- **Hard cuts**, about every 2–3 seconds, timed to your sentences.
- **Cropped cards.** Each shot shows the important part of a panel (a face, the
  action) as a card with a white border and a black shadow.
- **Layouts.** Sometimes one card sits in the centre. Sometimes it slides left
  while a second card slides in from the right. Sometimes a small card sits
  over a blurred close-up.
- **Backgrounds** switch between a blurred copy of the panel and black.
- **Calm CapCut-style entrances** (Fade In, Slide Up, Blur In, Zoom In, Fit in
  Frame, Swing Right, Pull Down) take 0.6–0.8s. The image then drifts slowly
  for a couple of seconds and holds still. Nothing is rushed.
- **Shake** happens only on big impact moments. Flash and zoom punch are off
  unless you turn them on under `video: effects:` in `manhwa-ocr.yaml`.
- **Art only, no text panels.** Panels that are only speech bubbles, captions,
  big lettering, blank pages or scanlator credits ("Read at…") are never shown.
  If the script tags one, the video shows the nearest drawn panel instead.
  Caption boxes at the top or bottom of an art panel are cropped out. Bubbles
  in the middle of the art are not erased.
- **Long sentences don't freeze on one picture.** A long sentence cuts through
  the panels it's describing, about every 3–4 seconds.
- **The video ends with your voice.** Silence at the end of your recording
  (TTS tools often add some) is left out, so the video is as long as what you
  actually say.

---

## Step 6 — Review and fix

**A build stopped, or a shot looks wrong?** Press **Copy agent prompt: fix chapter** on the dashboard, or
paste this prompt with the chapter number and the exact error (or the wrong shot ids) filled in:

```text
Read AGENTS.md first.
Work only on the series "The Hero Cannot Rest". Put --session "the-hero-cannot-rest" on every command. Copy commands exactly.
Run only `python -m manhwa_ocr ...` commands.
Never import manhwa_ocr in `python -c`. Never read or patch the program's source.
Never edit anything under manhwa_ocr\, tests\ or the program root.
Never create, convert, trim or generate audio. If audio is missing, stop and tell me the path.
Never rewrite a recap whose chapter already has audio unless I ask.
If a command fails or its output is confusing, STOP and show me the exact command and the exact error.
Do not work around a tool bug by hand-editing generated files (alignment.json, plan.json timestamps or panels, panels.json).
Keep a log: append every manual edit you make (file, what, why) to .work\agent_log.md.
Ask me before running anything with --force that discards edits.

Fix chapter 8.
What is wrong (the exact error or the shot list):
(paste the exact error, or the shot ids that are wrong)

Fix it with ONLY these routes.
- Wrong picture: pairs.json or recaps\chapter_8.cues.json, then `video align --chapter 8`, then `video plan --chapter 8 --force`.
- Framing (face, crop, drop): video\chapter_8.fixes.json only. Valid JSON, exact panel ids,
  allowed shapes 9:16, 3:4, 1:1, 4:3, 16:9.
- Never edit cards[k].panel. Never script-edit plan files. Never hand-edit timestamps.
  If the problem is a timestamp, stop and report the chapter, the sentence numbers and the times.
Then run `python -m manhwa_ocr video verify --chapter 8 --session "the-hero-cannot-rest"`,
`python -m manhwa_ocr video approve --chapter 8 --session "the-hero-cannot-rest"` and
`python -m manhwa_ocr video render --chapter 8 --session "the-hero-cannot-rest"`.
Do not use --force on render to skip a review or sync gate.
```

The agent fixes pictures in `pairs.json` and framing in `fixes.json`, then verifies, approves and renders. It
never hand-edits the plan or the timestamps, and it never forces a render past a review or sync gate.

**Pictures don't match the words?** That's the pairing. Run
`python -m manhwa_ocr video pair --chapter N` and open the sheets it lists
(in `.work\video\chN\`): every sentence is shown with its panel (green) and
the next panels in the story (grey), so a panel that's one off is easy to see.
Fix the numbers in `pairs.json`, then run, in order:
`video pair --chapter N --apply`, `video align --chapter N` (this is what
actually decides which picture shows — it must pass its own validation before
the next step will run), `video plan --chapter N --force`, and
`video render --chapter N`.

**Render refuses: "sync gate"?** Before rendering, the plan is audited against
your recording: total wrong-picture time, the worst five-second windows, and
cuts that land too late. A plan that fails stops with the numbers. The full
report is `python -m manhwa_ocr video sync-audit --chapter N` (also written to
`video\chapter_N.syncaudit.json`), with the worst stretches and their causes.
Fix the pairing (above) and render again. `--force` on render bypasses the
gate; use it only after reading the storyboard and the audit report.

The video always follows these rules: each panel is shown once, panels keep
their full width (only top and bottom are trimmed), text-only panels are never
used, and caption boxes and bubbles at a panel's top or bottom are cut off.

On the dashboard's **Video** tab, each chapter has:

- **Storyboard:** every shot with its time and the sentence spoken over it.
- **Preview** / **Video:** play it in the browser. You can seek.

If a shot has the wrong *picture*, that's a pairing/alignment fix (see above),
not a plan edit. Everything else (framing, background, entrance, a panel that
shouldn't show) goes in `video\chapter_N.fixes.json`. `video\chapter_N.plan.json`
is presentation only: `video plan --force` rebuilds it from the alignment and
applies `fixes.json` every time, so anything typed into the plan is lost.
**Only the changed shots are redrawn, so a fix takes seconds.**

| To fix… | Change | Then |
|---|---|---|
| wrong picture | `pairs.json` for that sentence (ids are on `media\chN\panels_sheet_*.png`) | `video align --chapter N`, `video plan --chapter N --force`, `video render --chapter N` |
| wrong part of the picture | a `crop` entry in `fixes.json` (`panel`, `shape` such as `3:4`, `box` `[left, top, right, bottom]`; the box must keep that shape) | `video plan --chapter N --force`, then `video render --chapter N` |
| head cut off | a `face` entry (`panel`, `top`, `bottom` of the head as fractions) | same |
| panel that shouldn't show | a `drop` entry (`panel`, `reason`) | same |
| animation or background | a `shot` entry (`panel`, `enter` such as `cut`, `fade_in`, `slide_up`, `blur_in`, `zoom_in`; `background` `blur` or `black`) | same |

`video plan --chapter N --force` rebuilds the plan from the alignment and applies
`fixes.json` again, so your fixes stay and any hand edit to the plan is gone. A
`fixes.json` with one bad entry is refused whole, with a message per problem.

---

## Step 5b — Review the video before rendering (storyboard check)

Building a video does not mean it is right. Each chapter's storyboard shows every
shot: the picture, the time, the panel id, and the narration sentence spoken over
it. Before rendering, someone has to read that storyboard against the narration.
There are two ways to do that. Use one, not both.

Both ways write the same file, `video/chapter_N.fixes.json`, and both finish with
`video approve`. Nothing else in the plan or the code is edited by hand.

### Before either way: the automatic check (no AI)

```powershell
python -m manhwa_ocr video verify --chapter 8 --session "manhwa-2"
```

This rebuilds the plan and checks every drawn panel on its pixels: lettering, painted
bubble patches, blank pages, and a head that would be cut. Each panel that fails gets a
clean crop if one exists. A panel with no character and no clean crop is dropped and
listed by panel id, with its reason. The full report is `video/chapter_8.verify.json`.

### Way A: an IDE agent (Antigravity, Claude Code, Cursor)

Build the storyboard, then paste this prompt into the agent. The same prompt is written
to `.work/video/ch8/REVIEW.md` next to the storyboard sheets:

```text
Read AGENTS.md first.
Work only on the series "manhwa-2". Put --session "manhwa-2" on every command. Copy commands exactly.
Run only `python -m manhwa_ocr ...` commands.
Never import manhwa_ocr in `python -c`. Never read or patch the program's source.
Never edit anything under manhwa_ocr\, tests\ or the program root.
Never create, convert, trim or generate audio. If audio is missing, stop and tell me the path.
Never rewrite a recap whose chapter already has audio unless I ask.
If a command fails or its output is confusing, STOP and show me the exact command and the exact error.
Do not work around a tool bug by hand-editing generated files (alignment.json, plan.json timestamps or panels, panels.json).
Keep a log: append every manual edit you make (file, what, why) to .work\agent_log.md.
Ask me before running anything with --force that discards edits.

You are reviewing the storyboard of a manhwa recap video for chapter 8 of the series "manhwa-2".
You run only `python -m manhwa_ocr ...` commands. Do not edit any program code. Do not delete any file.
Do not hand-edit alignment or plan files. You may only write video/chapter_8.fixes.json.
Framing fixes survive re-planning only in fixes.json.

1. Open every storyboard sheet: video/chapter_8.storyboard_01.png, _02, _03 (and any more the folder has).
   Open .work/video/ch8/NARRATION.md. It lists each shot's time, panel ids and the sentence spoken over it.
   Read it all first.

2. For EVERY shot, compare the picture with the sentence spoken over it.

3. Look for these problems in every shot:
   a. A WRONG PICTURE: the picture does not show what the sentence describes (wrong character, wrong place, unrelated art). Note the shot id.
   b. TEXT on screen: speech-bubble dialogue, captions, sound-effect lettering, a logo, a watermark (for example ASURASCANS.COM), a "Read at" credit, or a title card. These must not be shown. Propose a "drop" for a panel that is only text or a logo.
   c. A WHITE or BLANK screen, or an empty wall, door, leaf or floor with no character and no story value. Propose a "drop" for it.
   d. WHITE GUTTERS at the edges of a card: a white or black band along the top, bottom or sides of the picture. Propose a "crop" that stays inside the art.
   e. SIDES CROPPED OFF: a card cut at the left or right so a character, hand or weapon is sliced. Propose a "crop" at full width, if one exists.
   f. A FACE or HEAD cut: half a face, a chin or forehead missing, a character cut at the eyes or nose. Propose a "face" entry with the head's top and bottom as fractions of the panel's height (0 is the top edge, 1 is the bottom edge), or a "crop" entry whose box keeps the whole head.
   g. A PAINTED PATCH: a flat grey or coloured blob where a bubble was removed. Propose a "crop" that avoids it, if one exists.
   h. An IMPORTANT panel that is missing: a character or story beat the narration describes but no shot shows. Name the panel ids you can see are missing.
   i. A DUPLICATE in a row, or a clearly wrong order.

4. Rules for fixes:
   - Drop only a panel that shows no character and no story detail. Never drop a panel with a character in it, even a small one.
   - A "crop" must keep one of these shapes: 9:16, 3:4, 1:1, 4:3, 16:9. The box is [x0, y0, x1, y1] as fractions of the panel (0 to 1), and must keep that shape.
   - Use the panel id exactly as the storyboard shows it, for example P016-06.

5. Write your fixes to video/chapter_8.fixes.json in this exact form. Keep the file valid JSON.
   Leave a list empty if you have nothing for it. "shot" is optional.

   {"version": 1,
    "drop": [{"panel": "P016-06", "reason": "sound-effect lettering only"}],
    "face": [{"panel": "P017-02", "top": 0.12, "bottom": 0.46}],
    "crop": [{"panel": "P018-05", "shape": "3:4", "box": [0.0, 0.10, 0.6, 0.85]}],
    "shot": [{"panel": "P019-01", "background": "black", "enter": "fade_in"}]}

6. Rebuild and look again:
     python -m manhwa_ocr video verify --chapter 8 --session "manhwa-2"
     python -m manhwa_ocr video storyboard --chapter 8 --session "manhwa-2"
   Then repeat from step 1 with the NEW storyboard images. Stop when every shot is right and no fixes are needed.
   Run at most 3 rounds.

7. When every shot is right, run `python -m manhwa_ocr video approve --chapter 8 --session "manhwa-2"`.
   If a shot cannot be made right, do not approve. Say which shot and why.

8. Your final message must list every shot you changed and why, and every dropped panel with its reason.
```

### Way B: the terminal (DeepSeek flash, no IDE agent)

The same review runs from the terminal. The reviewer is the vision model in
`manhwa-ocr.yaml` (`provider:`), which is DeepSeek flash by default.

**One-time setup**

1. Get an API key at platform.deepseek.com and copy it (it starts with `sk-`).
2. Set it for your Windows user (open a new terminal after this):

   ```powershell
   setx DEEPSEEK_API_KEY "sk-paste-your-key-here"
   ```

   To try it for one terminal session only, use instead:

   ```powershell
   $env:DEEPSEEK_API_KEY = "sk-paste-your-key-here"
   ```

3. Check `manhwa-ocr.yaml`. These lines are the defaults:

   ```yaml
   provider:
     kind: openai
     vision_model: deepseek-flash      # reads the storyboard images
     text_model: deepseek-chat         # text-only tasks
     base_url: https://api.deepseek.com
     api_key_env: DEEPSEEK_API_KEY

   video:
     require_review: false             # true: a video must be approved before rendering
     review:
       max_rounds: 3                   # review-and-fix rounds before giving up
       model: null                     # null = vision_model above; or set another vision model
   ```

**Run it**

```powershell
# Show what the reviewer would say, apply nothing:
python -m manhwa_ocr video review --chapter 8 --session "manhwa-2" --dry-run

# Review, apply its fixes, rebuild, review again; approve when it is clean:
python -m manhwa_ocr video review --chapter 8 --session "manhwa-2"

# Allow more rounds:
python -m manhwa_ocr video review --chapter 8 --session "manhwa-2" --max-rounds 5
```

Each round: the reviewer reads the storyboard sheets and the narration, and replies
with JSON (`approved`, plus `drop`, `face`, `crop` entries, and notes). Those fixes are
checked against the chapter's real panels and allowed shapes. A reply with a bad entry
is refused as a whole and nothing is applied. The plan is then rebuilt. The loop stops
when the reviewer approves, or after `max_rounds`. If the reviewer never approves,
nothing is approved, and the notes say which shots and panels remain.

Then render:

```powershell
python -m manhwa_ocr video render --chapter 8 --session "manhwa-2"
```

**Notes**

- A reviewer cost is one call per round, with each storyboard sheet as an image.
- The check is only as good as the model. Look at the approved storyboard yourself
  before publishing.
- If the model does not accept images, the run stops with a message. Pick a vision
  model in `provider:` or set `video.review.model`.
- `--force` does not skip the review gate. With `require_review: true`, a render
  needs an approval for the current plan.
- It does bypass the estimated-timing stop and the sync gate. Use it only after
  reading the storyboard and the `video sync-audit` report.

**Summary of the commands**

| Command | What it does |
|---|---|
| `video verify --chapter N` | rebuild the plan and run the automatic check (no AI) |
| `video storyboard --chapter N` | rebuild the storyboard sheets and NARRATION.md |
| `video review --chapter N` | DeepSeek (or your vision model) reviews, fixes, and approves |
| `video approve --chapter N` | approve the current plan by hand |
| `video sync-audit --chapter N` | audit the current plan against the recording (no AI); the check a render gates on |
| `video render --chapter N` | render the MP4 (needs approval if `require_review` is true; refuses a plan that fails the sync gate) |

## The intro (chapter 0)

A short trailer in front of the series, built from panels of any chapters. Run
`python -m manhwa_ocr video intro --prepare` to get a template `intro\intro.json`
and contact sheets of every chapter's drawn panels (`intro\sheets\`). Write one
entry per narrated sentence: its text and one to three panels such as
`ch2:P010-14`. A panel may be used once, and text-only panels are rejected. Mark a
big moment with `"beat": true` (shake, flash, fade-out). Record that narration as
`audio\chapter_0.mp3`, then run `python -m manhwa_ocr video intro` to get
`video\chapter_0.mp4`. Pictures hold at least 2 seconds (`video: intro:` in
`manhwa-ocr.yaml`). Framing fixes go in `video\chapter_0.fixes.json`, using the
same `ch2:P010-14` ids.

To have an agent do it, paste this (or press **Copy agent prompt: intro**):

```text
Read AGENTS.md first.
Work only on the series "The Hero Cannot Rest". Put --session "the-hero-cannot-rest" on every command. Copy commands exactly.
Run only `python -m manhwa_ocr ...` commands.
Never import manhwa_ocr in `python -c`. Never read or patch the program's source.
Never edit anything under manhwa_ocr\, tests\ or the program root.
Never create, convert, trim or generate audio. If audio is missing, stop and tell me the path.
Never rewrite a recap whose chapter already has audio unless I ask.
If a command fails or its output is confusing, STOP and show me the exact command and the exact error.
Do not work around a tool bug by hand-editing generated files (alignment.json, plan.json timestamps or panels, panels.json).
Keep a log: append every manual edit you make (file, what, why) to .work\agent_log.md.
Ask me before running anything with --force that discards edits.

1. Run `python -m manhwa_ocr video intro --prepare --session "the-hero-cannot-rest"`. Open the contact sheets it writes.
2. Fill intro\intro.json. Give every sentence 1 to 3 drawn panel references written like "ch2:P010-14".
   Use each panel once. Never use a text-only panel.
3. Build it: `python -m manhwa_ocr video intro --session "the-hero-cannot-rest"`.
4. Then follow the same verify, review, approve and render steps as the build routine, for chapter 0.
Do not build the intro by hand-copying images, and do not write alignment or plan files yourself.
```

---

## Step 7 — Finish: one long video, Shorts, the full script

```bash
python -m manhwa_ocr video compile --chapters 1-7          # one video, title cards between chapters
python -m manhwa_ocr video clip --chapter 3 --from 45 --to 105 --format 9:16   # a hand-cut YouTube Short
python -m manhwa_ocr video shorts --chapter 3               # 3 auto-cut Shorts + shorts.json manifest
python -m manhwa_ocr video endscreen --chapter 3            # endscreen.json + watch_next.txt
python -m manhwa_ocr compile --chapters 1-7                # one long script, with a runtime table
```

These write `video\compiled_1-7.mp4`, `video\chapter_3.clip_45-105_9x16.mp4`,
`video\chapter_3.shorts_1..3.mp4`, `video\chapter_3.shorts.json`,
`video\chapter_3.endscreen.json`, `video\chapter_3.watch_next.txt`, and
`recaps\compiled_1-7.md`.

**Background music:** set `music: "music/bed.mp3"` under `video:` in
`manhwa-ocr.yaml` (the path is relative to the series folder). It gets quieter
automatically whenever you speak.

---

## The dashboard

`python -m manhwa_ocr ui` shows everything without hunting through folders:

- **Every series** in the sidebar, with progress. Start a new one with **+**.
- **Next step:** the same as `next`, plus three buttons that copy ready-made
  Antigravity prompts: *transcribe*, *recaps*, *build videos*.
- **Chapters:** each chapter's link, transcript and recap. Filter them, tick
  some, and copy a recap or compile command for exactly those.
- **Recap scripts:** compiled scripts.
- **Video:** script to record, upload audio, sync quality, storyboard, preview,
  final video.
- **Characters:** the series memory (who's who, open plot threads).
- **Add links:** paste new chapter links.

The dashboard never runs the work itself. That stays in your agent, so only one
thing is ever doing the work.

---

## Keeping agents out of the program

AI agents are allowed to **run** the program, never to **change** it. Four layers
make sure of that:

1. **Open Antigravity in `Documents\ManhwaRecaps`**, your output folder. The program
   isn't in its workspace, so it has no reason to touch it.
2. **Rules it reads first.** `GEMINI.md` and `AGENTS.md` in both folders (and
   `.agent\rules\` in the program folder) tell it never to edit the program, and to
   stop and tell you when something goes wrong instead of patching code.
3. **The lock.** The program's files are read-only, and every command first checks
   them against a fingerprint. If anything was changed, nothing runs: the error lists
   the files, so an agent can't quietly carry on with edited code.
   ```bash
   python -m manhwa_ocr guard status    # is the program exactly as locked?
   python -m manhwa_ocr guard unlock    # only when YOU want to change the code
   python -m manhwa_ocr guard lock      # lock it again afterwards
   ```
4. **Git, the undo button.** The program folder is a git repository. If something
   ever changed it, this brings it back exactly:
   ```bash
   cd C:\Users\akash\Documents\manhwaOCR
   git status                      # what changed
   git checkout -- . && git clean -fd manhwa_ocr tests
   ```

No guard can stop a program that runs as you from undoing it on purpose, but
together these turn a silent change into a loud stop that's one command to undo.

## Later: new chapters, other series

**New chapters released:**

```bash
python -m manhwa_ocr links add "https://.../chapter/8"
```

Then paste the step 2 prompt again. It continues at chapter 8 with all the
character memory intact. Then repeat steps 3–5 for the new chapter.

**Another series:**

```bash
python -m manhwa_ocr start "Some Other Series"           # new series, becomes active
python -m manhwa_ocr session list                        # * marks the active one
python -m manhwa_ocr session use "The Hero Cannot Rest"  # switch back
```

Switching back restores everything exactly as you left it.

**Transcripts you made before using this tool** (like the `chapter_N_ocr.md`
files you already have):

```bash
python -m manhwa_ocr session use "The Hero Cannot Rest"
python -m manhwa_ocr import "C:\path\to\folder-with-them"
```

Files named `chapter_N.md` or `chapter_N_ocr.md` come in as chapter N. Their page
images aren't kept, so `video build` downloads them again from `chapters.txt`.
If that's blocked, point it at saved images:
`video panels --chapter 2 --pages "C:\path\to\chapter-2-images"`.

---

## Settings worth knowing

All in `manhwa-ocr.yaml` in the program folder; changes are picked up next run.

| Setting | What it does |
|---|---|
| `video.format` | `16:9` (YouTube) or `9:16` (Shorts) |
| `video.target_shot` | average seconds per shot (default 2.7) |
| `video.effects` | `shake`, `punch`, `flash`, `end_fade` on/off |
| `video.music`, `video.music_volume` | background music bed |
| `video.whisper_model` | `small.en` (default), `base.en` is faster; `small` for Hindi |
| `video.whisper_language` | e.g. `hi` for Hindi narration |
| `video.sync.max_unmatched` | stop when this many seconds of narration have no trustworthy timing (default 6) |
| `video.sync.gate_wrong`, `gate_window`, `gate_grace` | the sync gate's tolerances (see Step 6) |
| `slicing.target_height` | lower to `1000` if transcripts have many `[unclear]` |

---

## Troubleshooting

| What you see | What to do |
|---|---|
| Not sure what to do next | `python -m manhwa_ocr next` |
| `No series selected` | `python -m manhwa_ocr session list`, then `session use <name>` |
| `No chapter links yet` | Paste links into `chapters.txt` (Step 1) |
| `JavaScript-rendered` / download failed | Save images with your browser, then `prepare --chapter N --folder <dir>` |
| Agent stops after one chapter | Paste the same prompt again; it resumes where it stopped |
| `A recap task ... is already in progress` | Finish it with `recap --finalize`, or throw it away with `recap --cancel` |
| `... is not valid JSON (line N, column M)` | A file the agent wrote is broken. Tell the agent to fix that line and re-run the same command. |
| `Needs attention` on the dashboard | Usually a broken line in `chapters.txt` (missing `https://`). Fix that line. |
| Transcript has lots of `[unclear]` | Lower `slicing.target_height` to `1000` in `manhwa-ocr.yaml` |
| Wrong character name throughout | Edit the `"name"` in `series_memory.json`. Later chapters use the fix. |
| Memory file got damaged | Copy `series_memory.prev.json` (the previous good version) back over it |
| `No narration audio for chapter N` | Step 4: upload it, or save `audio\chapter_N.mp3` |
| Recap shows **Untagged** on the Video tab | Written before tagging existed. Re-run step 3 for it with `--force`, then record again. |
| Plan says **Estimated timing** | `pip install -e ".[sync]"`, then `video align --chapter N` and `video plan --chapter N --force` |
| Plan says **Old style** | Made by an older version. `video build --chapters N` rebuilds it; if you had edited it, use `video align --chapter N` then `video plan --chapter N --force`. |
| `has no alignment yet` | Tagged chapters need `video align --chapter N` run before `video plan` — this is expected, not an error in your setup; `video build` does it for you automatically. |
| `alignment failed validation` | `video align --chapter N` found a structural problem (a missing panel, a timing gap, an unexplained jump). It prints exactly which sentence and why; fix that in `pairs.json` and re-run `video align`. |
| `... never reviewed` when rendering | `video align` flagged some sentences it wasn't fully sure about. Run `video pair --chapter N`, look at the sheets, fix `pairs.json`, then `video pair --chapter N --apply`, `video align --chapter N`, `video plan --chapter N --force`. If you have already looked at them, `video render --strict` is what stops on them; a plain render only warns. |
| Video is longer than your narration, or ends on a blank/credits page | Fixed in the current version: rebuild with `video align --chapter N` and `video plan --chapter N --force`, then `video render --chapter N` |
| Speech bubbles or text-only panels in the video | Same: `video align --chapter N` then `video plan --chapter N --force` (the panel check improved). Contact sheets mark them "TEXT - don't use". |
| Pictures drift out of sync with your voice | Make sure you recorded the *current* `tts.txt`. Then `video align --chapter N`, `video plan --chapter N --force` and `video render --chapter N`. |
| Right picture, wrong moment it appears | That's timing, not selection — check `video status` for "estimated" (see above). If it says "synced", it's a pairing issue: see "Pictures don't match the words" in Step 6. |
| Render refuses with `sync gate` | The pictures don't match your voice (wrong-picture time or late cuts). Run `video sync-audit --chapter N` for the worst stretches and causes, fix the pairing (Step 6), then render again. |
| `the plan was edited ... left alone` | You changed the audio or recap after editing the plan. Re-run `video align --chapter N` (so the alignment reflects the new audio/tags), then `video plan --chapter N --force` rebuilds the plan (your plan edits are lost). |
| Panels split strangely | Check `media\chN\panels_sheet_*.png`. Tune `video.panels` in `manhwa-ocr.yaml`, then `video panels --chapter N --force` |
| Want to start a series over | `session archive "<name>" --yes`, then `start` |
| Something else crashed | Re-run with `MANHWA_DEBUG=1` set to see the full error |

Nothing in this tool deletes your work. `session archive` moves a folder aside
and prints where it went. Memory saves keep a backup.

Downloaded pages and slices are cleaned automatically once a chapter is done.
`python -m manhwa_ocr clean` shows any leftover scratch; `clean --yes` removes it.

---

## A note on what you're transcribing

Comics are copyrighted, and most reader sites prohibit automated access in their
terms of service. Keep this pointed at material you have the right to process,
and don't republish transcripts of licensed work. The tool keeps a polite delay
between page downloads by default; leave it on.
