# External Editor Export Workflow — Fast-Track AI Mapping for CapCut & InShot

This is the canonical guide for AI coding agents to map manhwa images directly to narration audio and export ready-to-edit project files for external editors (**CapCut Desktop**, **CapCut Mobile**, **InShot**, **DaVinci Resolve**, **Premiere Pro**).

> [!IMPORTANT]
> **CRITICAL AGENT RULE: DO NOT RENDER IN THIS WORKFLOW.**
> In this workflow, agents **NEVER** run `video render`, `video verify`, `video review`, `video approve`, or `video qc`.
> Built-in ffmpeg video rendering is completely bypassed. The AI's job is to analyze panels, align them to the voice timestamps, plan shots, and **export the project**. The human creator performs all creative editing (transitions, keyframe animations, audio effects, B-roll) inside CapCut or InShot.

---

## Why This Workflow Exists

1. **Better Final Quality**: Built-in ffmpeg video editing can feel stiff and robotic. Professional NLEs (CapCut, InShot, Premiere) offer hardware acceleration, smooth easing curves, dynamic zooms, sound design, and text styling.
2. **Speed & Efficiency**: Rendering a 10-minute 1080p video with complex pan/zoom filters can take 15–30 minutes and consume huge compute. Alignment and export take **under 10 seconds**.
3. **No Review Bottlenecks**: Agents do not need to extract frames, check motion liveliness, or execute vision QC loops. The editor gets instant creative control.

---

## Workflow Comparison: Old Render Ladder vs. New Export Flow

| Stage | Old Built-in Render Flow | New External Editor Export Flow |
|---|---|---|
| **0. Transcribe** | `run --next` or `prepare/assemble/finalize` | `run --next` or `prepare/assemble/finalize` |
| **1. Recap & Tag** | `recap --prepare` → write `parts.json` → `recap --finalize` | `recap --prepare` → write `parts.json` → `recap --finalize` |
| **2. Audio** | User records `audio/chapter_N.mp3` or `video tts` | User records `audio/chapter_N.mp3` or `video tts` |
| **3. Pair & Cues** | `video pair --chapter N` → `video pair --chapter N --apply` | `video pair --chapter N` → `video pair --chapter N --apply` |
| **4. Align & Plan** | `video align --chapter N` → `video plan --chapter N --force` | `video align --chapter N` → `video plan --chapter N --force` |
| **5. Deliverable** | `video verify` → `video review` → `video approve` → `video render` → `video qc` (SLOW) | **`video export --chapter N --format all --session "<SERIES>"` (INSTANT)** |
| **6. Agent Action** | Debug rendering artifacts, re-render | **STOP. Notify the user that the project is exported.** |

---

## Step-by-Step Agent Execution Guide

Follow these steps in strict sequence for each chapter. Always supply `--session "<SERIES>"` explicitly.

### Step 0: Transcription
Produce the chapter transcript and detect panels:
```bash
python -m manhwa_ocr run --next --session "<SERIES>"
```
Verify that `chapter_N.md` and `media/chN/panels.json` exist before proceeding.

### Step 1: Recap Generation with Panel Tags
Generate the recap writing brief:
```bash
python -m manhwa_ocr recap --session "<SERIES>" --chapters N --prepare
```
- Read the generated `TASK.md`.
- Inspect the panel contact sheets in `media/chN/panels_sheet_*.png`.
- Write `.work/recap/parts.json` containing `"narration"`, `"hook"`, and `"summary"`.
- **CRITICAL TAGGING RULE:** Tag at least 95% of sentences with panel IDs (`{P001-02}`, `{P003-05}`, etc.). The video alignment and export place images on the timeline based directly on these tags.
- Finalize recap:
```bash
python -m manhwa_ocr recap --session "<SERIES>" --finalize
```

### Step 2: Narration Audio
Ensure narration audio exists for Chapter N:
- **Option A (Human recording):** The user saves the recorded voice-over to `<session>/audio/chapter_N.mp3` (or `.wav`, `.m4a`).
- **Option B (TTS synthesis):** Synthesize narration via Deepgram/TTS:
```bash
python -m manhwa_ocr video tts --chapter N --session "<SERIES>"
```
Check that `audio/chapter_N.*` exists.

### Step 3: Pairing & Cues Validation
Ensure panel tags correspond to genuine story beats:
```bash
python -m manhwa_ocr video pair --chapter N --session "<SERIES>"
python -m manhwa_ocr video pair --chapter N --apply --session "<SERIES>"
```

### Step 4: Voice-to-Panel Alignment & Shot Planning
Align narration words with sentence timestamps and generate the shot layout:
```bash
python -m manhwa_ocr video align --chapter N --session "<SERIES>"
python -m manhwa_ocr video plan --chapter N --force --session "<SERIES>"
```
This produces `video/chapter_N.plan.json`, containing microsecond-precise start and end times, panel assignments, and crop regions for every scene.

### Step 5: Export to External Editor
Run the export command:
```bash
python -m manhwa_ocr video export --chapter N --format all --session "<SERIES>"
```
Options for `--format`:
- `capcut`: Native CapCut Desktop draft project.
- `folder`: Universal sequenced image folder with SRT, CSV, and EDL files (InShot, CapCut Mobile, DaVinci Resolve, Premiere Pro).
- `all` (default): Exports both CapCut Desktop draft and Universal folder.

### Step 6: Agent Handoff (STOP HERE)
> [!CAUTION]
> **DO NOT PROCEED TO RENDER.**
> Do NOT execute:
> - `python -m manhwa_ocr video verify`
> - `python -m manhwa_ocr video review`
> - `python -m manhwa_ocr video approve`
> - `python -m manhwa_ocr video render`
> - `python -m manhwa_ocr video qc`
>
> Your job for this chapter is complete! Report the paths of the exported files to the user and stop.

---

## What `video export` Generates

### 1. Native CapCut Desktop Draft
- **Location:** `%LOCALAPPDATA%\CapCut\User Data\Projects\com.lveditor.draft\Recap_<SERIES>_Ch<N>`
- **Contents:**
  - `draft_content.json`: CapCut timeline containing:
    - Main video track with each manhwa panel pre-cropped and timed to narration sentence boundaries down to microseconds.
    - Synchronized audio track referencing the narration file.
    - Normalized canvas positioning.
  - `draft_meta_info.json` & `root_meta_info.json`: CapCut draft metadata including project cover thumbnail, duration, and hardware compatibility tags.
  - `assets/`: Pre-cropped, high-resolution panel images (`shot_001_P001-02.png`, etc.).
  - Sidecar template configs (`timeline_layout.json`, `template.tmp`, etc.) to guarantee instant loading without path errors.
- **Result:** CapCut Desktop detects the project immediately under "Drafts" on the home screen.

### 2. Universal Sequence Folder (for InShot / Mobile NLEs)
- **Location:** `<session>/export/chapter_<N>/`
- **Contents:**
  - `images/`: Sequentially ordered, pre-cropped panel PNGs named:
    `001_P001-04_00m00s_00m04s.png`
    `002_P002-01_00m04s_00m09s.png`
  - `timeline.srt`: Subtitle file with exact timestamps matching the narration sentence for each shot.
  - `timeline.csv`: Spreadsheet containing:
    `Index, Shot_ID, Start_Sec, End_Sec, Duration_Sec, Panel, Image_File, Narration`
  - `timeline.edl`: Industry-standard CMX 3600 Edit Decision List compatible with Premiere Pro, DaVinci Resolve, and Final Cut Pro.
  - `narration.<ext>`: A direct copy of the voice-over audio file.

---

## User Guide: Opening and Editing Projects

Provide this information to the user when completing an export:

### Editing in CapCut Desktop
1. Open **CapCut Desktop** on PC.
2. Under **Drafts** / **Recent Projects**, click **`Recap_<SERIES>_Ch<N>`**.
3. The project opens with:
   - The entire narration audio track in place.
   - Every panel image placed at the exact second the narrator speaks about it.
4. You can now add CapCut transitions (flash, burn, glitch), Ken Burns keyframes, automatic captions, sound effects, and background music.

### Transferring CapCut Project to Android / iOS
1. In **CapCut Desktop**, find the project card for `Recap_<SERIES>_Ch<N>`.
2. Click the **Cloud Space** icon (☁️ "Back up to Space / Cloud").
3. Wait a few seconds for the upload to complete.
4. Open the **CapCut App** on your Android or iPhone (logged in with the same TikTok/CapCut account).
5. Go to the **Cloud / Space** tab.
6. Tap the uploaded project and select **Edit / Download**.
7. The entire timeline with synchronized panels and audio is now ready for editing on mobile!

### Importing into InShot (Android / iOS)
1. Copy or send the `<session>/export/chapter_<N>/` folder to your phone (via USB cable, Google Drive, OneDrive, or LocalSend).
2. Open **InShot** → Tap **Video** → **New**.
3. Go to the **Photo** tab and select all images from the `images/` folder (they are numbered `001_...`, `002_...` so they stay in exact story order).
4. Tap **Music** → **Tracks** → **Import** and select `narration.mp3` from the exported folder.
5. Refer to `timeline.csv` or `timeline.srt` to adjust individual photo durations if needed.

---

## Agent Troubleshooting Checklist

| Issue | Cause | Fix |
|---|---|---|
| `No video plan found for chapter N` | `video plan` was not executed. | Run `python -m manhwa_ocr video align --chapter N` followed by `python -m manhwa_ocr video plan --chapter N --force`. |
| `Plan for chapter N contains no shots` | Recap narration had no panel tags. | Check `recaps/chapter_N.md`. Ensure sentences contain `{Pxxx-xx}` tags, then re-run `video pair --apply`, `video align`, and `video plan --force`. |
| CapCut draft not found on PC | CapCut Desktop is not installed in standard `%LOCALAPPDATA%` path. | The universal folder export is still created at `<session>/export/chapter_N/`. Direct the user to import the folder or set a custom path. |
| User requests changes to panel choice | A sentence shows the wrong character/moment. | Edit `.work/video/chN/pairs.json`, run `python -m manhwa_ocr video pair --chapter N --apply`, re-align, re-plan, and re-export. |


---

## Required Agent Delivery Format & Honest Residues Reporting

When completing `video export`, agents must provide a clear report following this exact structure:

### 1. Ready-to-Open Status (CapCut Desktop)
Emphasize to the user that **manual CSV duration adjustment is NOT needed in CapCut Desktop**:
> *"In CapCut Desktop, you do NOT need to manually import images or adjust clip durations from CSV! The project `Recap_<SERIES>_Ch<N>` is already generated under Drafts with all panels placed, pre-cropped, and trimmed at the exact second and microsecond mark on the timeline."*

### 2. Mobile Import Instructions (InShot & CapCut Mobile)
Explain the fallback for mobile editors:
> *"To import in **InShot**: use the Photo tab to import the `images/` folder in order (they are sorted `001_...`, `002_...`), add `narration.<ext>`, and use `timeline.srt` / `timeline.csv` as your timing reference."*
> *"To edit on **CapCut Mobile**: simply click the Cloud Space icon on the draft card in CapCut Desktop to back it up, then open CapCut Mobile and tap 'Edit' under Space."*

### 3. Honest Residues & Alignment Audit
Audit the alignment and report any residues so the creator knows what to expect:
- **Total Shots Aligned**: e.g., *"94 shots mapped across 160 panels."*
- **Unanchored / Interpolated Sentences**: List any sentence numbers whose word timestamps had to be interpolated from their neighbours (e.g. *"Sentences 8, 9, 13 were interpolated from adjacent cues; nudge them in CapCut if any feels off."*).
- **Trailing Silence**: Report the exact audio length vs timeline duration (e.g. *"The timeline ends at 462.7s against the 464.68s audio, leaving a 1.98s trailing tail of silence."*).
