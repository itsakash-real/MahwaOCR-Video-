# Make a recap video: step by step (decision tree)

Open a terminal in the program folder: `C:\Users\akash\Documents\manhwaOCR`.
Every command below has `--session "manhwa-2"`. Change `manhwa-2` to your series folder name.
Change `8` to the chapter number you want.

```
START
│
├── Q1. Do you have the narration audio for the chapter?
│     │
│     ├── NO  ──► Record recaps\chapter_8.tts.txt and save it as
│     │          ManhwaRecaps\manhwa-2\audio\chapter_8.mp3
│     │          Then come back here.
│     │
│     └── YES ──► go to Q2
│
├── Q2. Run the build
│     │
│     │   python -m manhwa_ocr video build --chapters 8 --session "manhwa-2" --force
│     │
│     │   This takes about 25 minutes. Wait for the last line.
│     │
│     ├── The last lines say "wrote video\chapter_8.mp4"  ──► go to Q3
│     │
│     ├── The last lines say "can't be built yet"           ──► go to Q5
│     │
│     └── Anything else (red text, "Error:")                ──► go to Q6
│
├── Q3. Check the video quickly (2 minutes)
│     │
│     │   python -m manhwa_ocr video clip --chapter 8 --from 60 --to 80 --session "manhwa-2"
│     │
│     │   Open  ManhwaRecaps\manhwa-2\video\chapter_8.clip_60-80.mp4  and watch it.
│     │
│     ├── Looks right (faces whole, pictures match the voice)  ──► go to Q4
│     │
│     └── Faces cut, or wrong pictures, or long still pictures ──► go to Q5
│
├── Q4. Make the full chapter video
│     │
│     │   It is already rendered:  ManhwaRecaps\manhwa-2\video\chapter_8.mp4
│     │   Repeat Q2 and Q3 for the next chapter.
│     │   Done when every chapter has a chapter_N.mp4.
│
├── Q5. Something is wrong with the pictures
│     │
│     ├── Option A (no AI, free) ──────────────────────────────────────────────┐
│     │     Run the same build again. Each run uses the same rules, so run it   │
│     │     only after you have changed a setting or a fix file.                │
│     │                                                                         │
│     ├── Option B (AI review, needs a DeepSeek key) ───────────────────────────┤
│     │     Only if you have set DEEPSEEK_API_KEY (see "Setup" below).          │
│     │       python -m manhwa_ocr video review --chapter 8 --session "manhwa-2" │
│     │     It looks at the storyboard, makes its own fixes, and rebuilds.      │
│     │     When it says "approved", render:                                    │
│     │       python -m manhwa_ocr video render --chapter 8 --session "manhwa-2"│
│     │                                                                         │
│     └── Option C (you decide, by hand) ──────────────────────────────────────┘
│           Open ManhwaRecaps\manhwa-2\.work\video\ch8\REVIEW.md and the
│           storyboard images in ManhwaRecaps\manhwa-2\video\.
│           Write fixes in ManhwaRecaps\manhwa-2\video\chapter_8.fixes.json
│           (see "Fix file" below), then run Q2 again.
│
├── Q6. Something failed (red text)
│     │
│     ├── "No narration audio"            ──► Q1, the audio file is missing or named wrongly
│     ├── "too short to hold N panels"   ──► the narration is shorter than the panels need;
│     │                                       record a longer narration or lower min_hold
│     ├── "whisper" / "estimated"          ──► the voice could not be matched; see "Setup"
│     ├── "not valid JSON" / "Error:"     ──► open the file named in the message, fix the
│     │                                       line it names, run Q2 again
│     └── anything else                    ──► copy the last 20 lines of the output and
│                                              send them to whoever maintains this tool
│
└── Q7. Which chapters are done?
      python -m manhwa_ocr video status --session "manhwa-2"
      "video" column says ok = done.  outdated = run Q2 for that chapter again.
```

## Setup (do once)

```
Setup
├── Python installed?              python --version   (3.10 or newer)
├── Program installed?             python -m manhwa_ocr --help   (shows a list of commands)
├── Video extras installed?        pip install -e ".[video,sync]"
└── AI review key (optional)
      setx DEEPSEEK_API_KEY "sk-your-key"
      then CLOSE the terminal and open a new one
```

## Fix file (for Q5 Option C)

Save as `ManhwaRecaps\manhwa-2\video\chapter_8.fixes.json`. Use only the panel ids shown
on the storyboard (for example `P016-06`).

```json
{
 "version": 1,
 "drop": [
   {"panel": "P016-06", "reason": "blank page, no character"}
 ],
 "face": [
   {"panel": "P017-02", "top": 0.12, "bottom": 0.46}
 ],
 "crop": [
   {"panel": "P018-05", "shape": "3:4", "box": [0.0, 0.10, 0.6, 0.85]}
 ]
}
```

- `drop`: remove a panel that shows no character and no story.
- `face`: the head's top and bottom as a fraction of the panel height (0 = top edge, 1 = bottom edge).
- `crop`: an exact box. `shape` is one of 9:16, 3:4, 1:1, 4:3, 16:9.

If the file has a mistake, the build says which line and does not apply any of it.
Then run Q2 again.

## Where your files go

```
ManhwaRecaps\
└── manhwa-2\
    ├── audio\            chapter_8.mp3          (you record this)
    ├── recaps\           chapter_8.tts.txt      (the text you read aloud)
    ├── media\ch8\        panel pictures (made automatically)
    └── video\            chapter_8.mp4          (the finished video)
                          chapter_8.fixes.json   (your fixes, optional)
                          chapter_8.verify.json  (the automatic check report)
```

## Quick rules

- Never delete files to get past an error. Fix the file the error names.
- Run Q2 again after any change to the fixes file, the audio, or the recap.
- Keep the chapter number and the series name exactly as in the folder names.
