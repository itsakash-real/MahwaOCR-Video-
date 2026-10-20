# Agent way — copy-paste prompts for another agent

Use these with Antigravity, OpenCode, Cursor, Claude Code, Aider, or any
agent that can run shell commands. One prompt per job. Paste one, let it
finish, then paste the next.

## The one rule for every prompt — do not retype prompt text

The authoritative prompt text for the base prompts A–F lives in exactly
one place, `manhwa_ocr/agent_prompts.py`. The house rule (see `AGENTS.md`)
is: never copy prompt text into other files. So each prompt A–F below
tells the agent *which prompt to read* from `agent_prompts.py`, plus the
current-workflow rules (CapCut fixes, captions, review gates) that the
agent must also follow. Get the base prompt text from one of these
sources, never from memory:

- Dashboard (`python -m manhwa_ocr ui`) — buttons copy ready-made prompts:
  *transcribe*, *recaps*, *build videos*.
- The file itself: `manhwa_ocr/agent_prompts.py` — prompts A–F.
- `USER_GUIDE.md` — section "Which prompt do I use?" lists them verbatim.

Letters: A = transcribe, B = recaps, C = build, D = review, E = intro,
F = fix. Prompt G (Shorts + end-screen + publish checklist), Prompt H
(Full review + rebuild with new fixes) and Prompt I (build/continue a wide
or adaptive-mode chapter video) are docs-level prompts defined below in
full — they are NOT in `agent_prompts.py`, because they are checklists /
rebuild / workflow-trigger procedures, not base prompts.

## Before you start (applies to every prompt)

1. Open the agent in your **output folder** (`Documents/ManhwaRecaps`), not
   in the program folder. The agent runs the program, never changes it.
2. The agent must use only `python -m manhwa_ocr ...` commands, always with
   `--session "Your Series Name"`, and the venv Python
   (`~/workspace/venvs/manhwa-ocr/bin/python` — adjust to your machine)
   for align/whisper steps (system Python lacks pywhispercpp, so align
   silently falls back to estimated timing and render refuses).
3. The agent must never import `manhwa_ocr` internals, never use
   `--workdir`, never create, convert, trim, or generate audio. If audio
   is missing, it stops and tells you the path.
4. The agent must never rewrite a recap whose chapter already has audio
   unless you ask. The recording would no longer match the script.
5. The agent must never hand-edit `video/chapter_N.plan.json`,
   `video/chapter_N.alignment.json`, or any generated file to work around
   a tool bug. Fixes go only through `pairs.json` / `cues.json` /
   `fixes.json`, then re-run align → plan → verify. If a command fails,
   the agent stops and shows you the exact command and the exact error.
6. **Bubble painting is removed entirely.** `clean_bubbles()` returns the
   panel unchanged (pixel-identical). There is no flag to re-enable
   painting. A panel whose best crop still shows a bubble is DROPPED and
   re-paired, never painted. No painted patch may ever land on a face.
7. **Do not touch the `main` branch pointer, do not force-push, do not
   rebase** unless the user explicitly says so in that message. If a bug
   in the program blocks the build, STOP, report the exact step +
   traceback, do NOT hotfix the program in place.

## Prompt A — Transcribe

```
Use prompt A (transcribe) from manhwa_ocr/agent_prompts.py for the series "Your Series Name".
Read AGENTS.md first. Follow that prompt exactly.
GOAL: scrape every chapter's panels into a transcript (OCR + what is happening in each image).
INPUTS: series "Your Series Name", chapters in order. Chapters must be processed in order (memory is order-dependent).
COMMANDS: drive only with `python -m manhwa_ocr <command>`, always with --session "Your Series Name". Never import internals, never --workdir.
RULES:
- For EACH image: extract ALL visible text (OCR, exactly as shown) + describe what situation is happening (who, doing what, where). Never invent events, numbers, dialogue, or names.
- Use panel ids exactly as shown (e.g. P004-05 = page 4, panel 5).
- Process chapters in order. Loop with `python -m manhwa_ocr next --session "Your Series Name"` until it tells you to stop.
- If a command fails, stop and show the exact command + exact error. Do not delete files to get past an error.
ACCEPTANCE: every chapter has chapter_N.md, every tile id from TASK.md has an entry, no invented content.
```

What it does: runs `next`, prepares / assembles / finalizes chapters in
order until every chapter is transcribed.

## Prompt B — Recaps

```
Use prompt B (recaps) from manhwa_ocr/agent_prompts.py for the series "Your Series Name", chapters 1-4, about 7 minutes per chapter.
Read AGENTS.md first. Follow that prompt exactly.
GOAL: write the recap narration script (panel-tagged) for each chapter, from its transcript.
INPUTS: series "Your Series Name", chapters 1-4 (or the chapters you name). Transcripts must already exist (Prompt A).
COMMANDS: `python -m manhwa_ocr recap --session "Your Series Name" --chapters N --prepare`, write .work/recap/parts.json, then `recap --finalize`. Never import internals, never --workdir.
RULES:
- No intro/outro. Exact plot / order / details from the transcript. Never invent events, numbers, dialogue.
- Names exactly as in SERIES CONTEXT. Third person only. The player is called "the streamer" / "KukuruppingPpong". Never I / my / me / we in narration, even if the source is first person.
- Casual, conversational, like telling a friend. Engaging, not boring. Just the script.
- Tag EVERY sentence with its panel ({P003-02}, {same}, {impact}). Target 95%+ tagged. Never tag a text-only, bubble-only, blank, or credits panel.
- Hook/open loops: open with a hook — first 30 seconds give proof (exact rank/number/beat the title promises), stakes, one-line roadmap. Mark it [HOOK] on its own line before the sentence. Open a loop every 2-3 min: [LOOP: the question] on its own line. End on an open question/cliffhanger. [HOOK] / [LOOP: ...] are editing cues, never spoken, never in tts.txt.
- Never rewrite a recap whose chapter already has audio unless the user asks.
ACCEPTANCE: recaps/chapter_N.md + .tts.txt exist, 95%+ sentences tagged, no first-person narration, no invented events.
```

What it does: `recap --prepare`, writes `parts.json` (original narration,
panel tags, `[HOOK]` / `[LOOP: ...]` markers), `recap --finalize`. Then it
stops and tells you to record each `recaps/chapter_N.tts.txt` as
`audio/chapter_N.mp3`.

## Prompt C — Build videos

```
Use prompt C (build) from manhwa_ocr/agent_prompts.py for the series "Your Series Name", chapters 1-4.
Read AGENTS.md first. Follow that prompt exactly.
GOAL: build the chapter video(s) from recorded narration audio + scraped panels, on current main (CapCut fixes included).
INPUTS: series "Your Series Name", chapters 1-4. Each chapter MUST already have audio/chapter_N.mp3 (recorded from recaps/chapter_N.tts.txt) and a recap. If audio is missing, STOP and tell me the path. Never create/convert/generate audio.
COMMANDS (in order, per chapter; venv Python for align): `python -m manhwa_ocr video pair --chapter N --session "Your Series Name"`, then `video pair --chapter N --apply`, then venv `video align`, then `video plan`, then `video verify`, then `video review` (<=3 rounds), then `video approve`, then `video render`. Never import internals, never --workdir, never hand-edit plan.json / alignment.json.
RULES (current editor, all in main):
- Pairing (see Prompt D for detail): change ONLY wrong sentences, keep panel count unless clearly wrong, avoid text-only/bubble-only/blank/credits, verify pairs key vs alignment segment text (pairs numbering != cues numbering).
- Pacing (impactful-only selection): time is scarce, panels are abundant — SELECT, don't cover. Max shots = floor(audio_seconds / 3). Min shot 3.0s, ever. One tagged panel per sentence, held full window if ≤8s. A sentence >8s splits into 2 shots (tagged ~65% + ONE impactful filler ~35% or punch-in); >15s max 3 shots. A filler ONLY if: named character + visible action/beat + visible face, not text/SFX/fragment/back-of-head/spoiler, max 1/sentence (2 if >15s), min 3s, never a repeat, never backwards order — else the tagged panel holds. Duplicate-tag sentences HOLD (no filler). Tagged coverage ≥80% (≤8s) / ≥60% (>8s) or the sync gate FAILS; a filler failing the impact test counts as wrong-picture. panels_shown/total is INFO ONLY, never success.
- {same} trap: if a sentence marked {same} hides its tagged panel (panel never displays), flip that sentence's same to False and re-run align -> plan -> verify -> sync-audit.
- Timing warnings: any timing_warnings entry means estimated timing. Read its fix and fix it. Never render on estimated timing. Render refuses estimated timing by design.
- Captions: new plans carry sentence-aligned captions (3-5 words at a time, key word yellow) in a dedicated black bottom bar below the cards (cards capped to top 92%). Never let a caption cover panel art. Old plans have none — run `video captions --chapter N` to add them, then render again.
- Motion/transitions: continuous Ken Burns, never hold, never repeat previous motion, never pan twice in a row. Hard cuts default, fade 10%, slide/zoom/whip/glitch only on impact/reveal beats (glitch max 1 per 60s). No 0.5s glitch, no pan-repeat swap.
- Faces: never mask a face, never paint a bubble, never cut a face at edges. Crop to exclude bubbles, else drop + re-pair. System-window panels get wide fill (>=90% frame width, >=1.33 aspect), never a small square.
- Panel diversity: never 3 identical cards in a row. Third showing must use a different panel or a different (punched-tighter) crop.
- P2 switch: never switch to square 1.0 in a wide layout.
- Wide mode (video.mode: wide, opt-in, landscape only): every card is full frame height (no letterbox above/below), >=6/16 of the frame wide (no thin slivers, pairs exactly 8/16 each), captions OVERLAY the art (no black bar), upscale capped at 1.5x. The motion zoom eats ~4% of the window height at its extremes (push_in end / pull_out start) -- a crop is only safe if heads, horns and bubble text clear the window edge by MORE than that at the worst moment. Measure reviewer crops from the actual panel pixels, never from thumbnails.
  The window is built to contain the whole figure (head AND the rest of the body connected to it -- wings, tail, legs), not just the head: a confident head detection does not by itself mean the body below it is included. Three self-check signals ride in the plan and the QA file, never silently: `wide_figure_risk` (a figure taller than any window the panel's width/upscale cap allows to fully contain -- still a valid window, just flagged), `wide_caption_risk` (the overlaid caption may cover panel text at the window's bottom), and `wide_reviewer_focal_ignored` (a `fixes.json` focal crop you set was NOT used as-is -- either the panel's own aspect is >= 6/16 so it is shown whole, or the focal itself is taller than any legal window and was re-anchored). Treat all three as review items, not failures -- if any lists a panel you are about to ship, look at that panel's rendered frame before approving.
- Adaptive mode (video.mode: adaptive, opt-in, landscape only): per SHOT, not the whole video -- a naturally wide/landscape panel (its own aspect >= video.adaptive.wide_threshold, default 4:3) gets the exact same wide-mode treatment as above (full frame height, edge-safe window, the same `wide_figure_risk`/`wide_caption_risk`/`wide_reviewer_focal_ignored` signals apply to it); a narrower panel keeps its classic crop/shape, just without the equal-area shrink that otherwise forces every shape onto the same small on-screen size regardless of how much visual weight its content actually carries. Pairs are always classic-style (pairing already excludes landscape panels). Captions always overlay the art for every shot in an adaptive video, never a bar -- that stays constant even though the card treatment itself changes shot to shot, so the two different presentation styles never visually fight each other.
- SFX/music: render mixes SFX from video.sfx_dir and music from music/bed.mp3 if present. Missing files -> warning, render without, never fail, never invent audio.
ACCEPTANCE: video/chapter_N.mp4 exists (1080p), sync gate PASS, spot-checks clean (see Prompt F). Report duration, file size, every shot changed + why, every dropped panel + reason, every pairs.json change.
```

What it does, per chapter: pair → pair --apply → align → plan → verify →
storyboard review (≤3 rounds) → approve → render. Then it reports
duration, file size, every shot changed + why, every dropped panel +
reason, every pairs.json change.

## Prompt D — Review a storyboard (pairing)

```
Use prompt D (review) from manhwa_ocr/agent_prompts.py to review the storyboard of chapter N of the series "Your Series Name".
Read AGENTS.md first. Follow that prompt exactly.
GOAL: fix picture-to-sentence pairings in pairs.json so every sentence shows the right picture.
INPUTS: series "Your Series Name", chapter N. Pairing sheets (pairing_*.png: GREEN panel = currently chosen, GREY = neighbours in story order), pairs.json (sentence number -> chosen panel id(s)), alignment segments.
COMMANDS: `python -m manhwa_ocr video pair --chapter N --session "Your Series Name"`, edit pairs.json only where wrong, then `video pair --chapter N --apply`, then venv `video align`, `video plan --force`, `video verify`. Never import internals, never --workdir, never hand-edit plan.json.
RULES:
- Change ONLY the sentences that are wrong. Leave correct sentences exactly as-is.
- Chosen panel MUST depict the sentence's subject (right character, action, location).
- Avoid panels that are only text, speech bubbles, blank, or credits.
- Use panel ids exactly as shown (e.g. P004-05). Keep the same number of panels per sentence (one or two) unless clearly wrong.
- NUMBERING TRAP: pairs.json numbering != cues.json numbering (cues splits sentences differently). Alignment segments use pairs numbering. ALWAYS verify a pairs key against the alignment segment text before editing pairs.json. Editing the wrong number changes the wrong sentence and fails the sync gate.
- {same} trap: if a sentence marked {same} hides its tagged panel, flip same to False and re-run align -> plan -> verify.
- Bubble rule: never paint a bubble. If the chosen panel's best crop still shows a bubble, crop to exclude it, else drop + re-pair.
ACCEPTANCE: list ONLY changed sentences (sentence number + old panel -> new panel + reason). Sync gate still PASS after re-plan.
```

Use it when you want a second pass on one chapter's storyboard without
rebuilding everything.

## Prompt E — Intro trailer (chapter 0)

```
Use prompt E (intro) from manhwa_ocr/agent_prompts.py for the series "Your Series Name".
Read AGENTS.md first. Follow that prompt exactly.
GOAL: build the 30-second intro hook video (chapter 0) from panels across chapters + recorded intro audio.
INPUTS: series "Your Series Name". You must already have intro/intro.txt (one sentence per line, [HOOK] / [LOOP: ...] markers) and audio/chapter_0.mp3 (30s, recorded from it). If audio is missing, STOP. Never create/trim/generate audio.
COMMANDS: `python -m manhwa_ocr video hook --chapters 1-6 --audio audio/chapter_0.mp3 --script intro/intro.txt --session "Your Series Name"` (or `video intro --auto` with same flags). Manual instead: `video intro --prepare`, fill intro/intro.json, `video intro`. Never import internals, never --workdir.
RULES:
- Hook structure: 0-10s proof ([HOOK]), 10-20s stakes, 20-30s roadmap + [LOOP: ...]. Missing markers warn, never block. No chapter cards, max 1 callout.
- Auto-pick: one panel per sentence, lexical overlap with panel OCR first + face/impact heuristics, never text-only/credits, never repeat, story order never backwards, tease ceiling keeps payoff panels out until sentence names the beat. Picks + reasons in intro/intro.auto.json (review record, not input — overrides via manual intro.json).
- Target 30s, 20-45s via video.hook config or --duration. Audio never trimmed/generated.
- Same editor rules as Prompt C: continuous motion, no bubble painting, captions in black bar, no face covered.
- Previously (between parts): write previously/previously_7.txt, record audio/previously_7.mp3 (30s), run `video previously --chapters 1-6 --audio audio/previously_7.mp3 --script previously/previously_7.txt`. Panels ONLY from chapters 1-6 (never the next part). Beats from previous part's RECAPS only (never transcripts).
ACCEPTANCE: video/chapter_0.mp4 exists (30s, 20-45s allowed), hook structure present, no repeated panels, no text-only/credits panels. Report picks (sentence -> panel + reason) + hook warnings.
```

What it does: `video intro --prepare`, you fill `intro/intro.json`, then
it aligns / plans / renders `video/chapter_0.mp4`. You record
`audio/chapter_0.mp3` first. Full rules: `INTRO_HOOK_GUIDE.md` +
`PREVIOUSLY_GUIDE.md`.

## Prompt F — Fix one chapter

```
Use prompt F (fix) from manhwa_ocr/agent_prompts.py to fix chapter N of the series "Your Series Name". The problem is: <describe it in one sentence>.
Read AGENTS.md first. Follow that prompt exactly.
GOAL: fix ONLY the stated problem in chapter N, nothing else.
INPUTS: series "Your Series Name", chapter N, problem in one sentence (e.g. "0.5s glitch on panels held >3s" / "caption covers panel text at 241s" / "same panel 3x in a row").
COMMANDS: fix only through pairs.json / cues.json / fixes.json, then re-run venv `video align`, `video plan --force`, `video verify`, `video sync-audit`. Never import internals, never --workdir, never hand-edit plan.json / alignment.json.
RULES:
- Change ONLY what the problem needs. Leave everything else exactly as-is. List ONLY changed sentences/panels.
- Use the current editor rules (Prompt C): no bubble painting (drop + re-pair), captions in black bar, continuous motion, no motion repeat, panel diversity, P2 never 1.0 in wide, system-window wide fill.
- If a command fails, STOP and show the exact command + exact error. Do not work around a tool bug by hand-editing generated files.
ACCEPTANCE: the stated problem is gone (verified by spot-check at the stated timestamp), sync gate still PASS, no other sentences changed.
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
5. Caption check: captions must be sentence-aligned (no gap >1s during narration, no caption >0.5s before its words), in the dedicated black bottom bar (never over art). If a caption covers panel text, STOP and report the timestamp.
6. Tell me exactly what to paste on YouTube: long-video title suggestion (formula: [Weak/Hook] + [Cheat/Twist] + [Shock Payoff] + [FULL | Manhwa Recap], under ~70 chars), description (first 2 lines hook + keywords, chapters + WATCH NEXT block), and one title + hook per Short.
7. Thumbnail checklist: 1 face ~40% of frame, max 3 words, yellow #FFD800 + thick black outline, arrow to the winner if 2 characters, no face covered by text, readable at 25% scale. 2-character story design preferred (shocked vs calm, small vs big power, question on top -> answer on bottom).
```

## Prompt H — Full review + rebuild with new fixes (NEW)

Prompt H is a docs-level prompt, not in `agent_prompts.py`. Paste it when
you want an agent to review + rebuild a chapter on updated `main` (CapCut
fixes + caption fixes + painting removal) and report honestly what fixed,
what still breaks, and any new issues. This is the exact workflow we
follow for a chapter rebuild.

```
You are reviewing + rebuilding ONE chapter on updated main. You have no prior context. Follow this exactly.

GOAL: rebuild chapter N of the series "Your Series Name" with the current code in main, WITHOUT overwriting the original video, and report honestly what fixed, what still breaks, and any new issues.

INPUTS:
- Series: "Your Series Name", chapter N.
- Session folder: <session path, e.g. ~/workspace/ManhwaRecaps/<series>/>.
- Original video: <session>/video/chapter_N.mp4 — DO NOT OVERWRITE IT. It is the baseline.
- Audio: <session>/audio/chapter_N.mp3 (already recorded, must exist — if missing, STOP and tell me the path; never create/convert/generate audio).
- Recap: <session>/recaps/chapter_N.md + .tts.txt + cues/parts (already finalised — do NOT rewrite the recap unless I explicitly say so, or alignment fails and I confirm).

SETUP:
1. On the repo, checkout main and pull: `git checkout main` then `git pull origin main`. Verify: `git log --oneline -3` must show the latest main HEAD (ask me for it if unsure). If main is not up to date, STOP.
2. Use ONLY `python -m manhwa_ocr <command>`, always with --session "Your Series Name". Use the venv Python (~/workspace/venvs/manhwa-ocr/bin/python — adjust to your machine) for align/whisper steps. Never import manhwa_ocr internals, never use --workdir.
3. Do NOT touch the main branch pointer, do NOT force-push, do NOT rebase, do NOT commit unless I explicitly say so. If a bug in the program blocks the build, STOP, report the exact step + traceback, do NOT hotfix the program in place.

SAFE OUTPUT:
4. Build to a SEPARATE output, never the original file. Either copy the session to a test session (e.g. <session>-test/) and build there, OR build in place with output name video/chapter_N_newcode.mp4 (use the tool's output override / test-session copy — whichever does NOT risk overwriting the original). Backup the original's plan/manifest first (copy plan.json to plan.oldcode.json) if you need to re-plan.
5. Re-plan with --force (painted cache v2 invalidates old painted entries). Reuse the existing recap/cues/pairs unless alignment fails — the ONLY variable is the new code.

BUILD (in order, per chapter):
6. `video pair --chapter N` — check every picture vs its sentence. Fix pairs.json ONLY where wrong (Prompt D rules: change ONLY wrong sentences, keep panel count unless clearly wrong, avoid text-only/bubble-only/blank/credits, verify pairs key vs alignment segment text — pairs != cues numbering).
7. `video pair --chapter N --apply`, then venv `video align`, then `video plan --force`, then `video verify`, then `video sync-audit`.
8. {same} trap: if a sentence marked {same} hides its tagged panel, flip same to False and re-run align -> plan -> verify -> sync-audit.
9. Timing warnings: any timing_warnings entry means estimated timing. Read its fix and fix it. Never render on estimated timing.
10. Storyboard review (Prompt E rules below) — review EVERY shot, ≤3 rounds. Then `video approve`, then `video render` to the SEPARATE output.

STORYBOARD REVIEW — check every shot for ALL of these (this is the review we follow):
- Pacing (impactful-only selection): max shots = floor(audio_seconds / 3), min shot 3.0s ever, one tagged panel per sentence (≤8s: tagged holds full window), >8s splits into 2 shots (tagged ~65% + ONE impactful filler ~35% or punch-in), >15s max 3 shots. Filler ONLY if named character + action/beat + visible face, not text/SFX/fragment/back-of-head/spoiler, max 1/sentence (2 if >15s), min 3s, never repeat, never backwards — else tagged holds. Duplicate-tag HOLD. Tagged coverage ≥80% (≤8s) / ≥60% (>8s). panels_shown/total is INFO ONLY.
- Text-only / blank / credits panels: DROP (never tag, never show).
- Bubbles: NEVER PAINT. Crop to exclude the bubble if a clean crop exists, else DROP the panel and re-pair. No painted patch may ever land on a face. Faces must never be cut at edges, never masked, never covered by text/effects.
- 0.5s glitch / freeze: no panel held >3s may read as a frozen cut. Motion must be continuous for the whole shot (no 2.6s ease + hold). Joins must be frame-exact, no 1-frame gap/overlap.
- Pan repeat: never repeat the previous motion, never pan twice in a row, at most 1 pan direction change per 8s same-panel stretch. Pair slide (pair_move) at most once per pair shot.
- Captions: sentence-aligned (no gap >1s during narration, no caption >0.5s before its words are spoken), in the dedicated black bottom bar (bottom 8%, cards capped to top 92%), never over panel art. Key word yellow. If a caption covers panel text, report the timestamp.
- Panel diversity: never 3 identical cards in a row. Third showing must use a different panel or a different (punched-tighter) crop.
- P2 switch: never switch to square 1.0 in a wide layout.
- System windows: system-notification / status-window panels get wide fill (>=90% frame width, >=1.33 aspect), never a small centred square. Check ~34s-style banner shots.
- Wide mode (video.mode: wide): same framing rule as Prompt C -- full frame height, >=6/16 wide, captions overlay the art. Final-render check: view the FIRST and LAST frame of EVERY shot plus a 1fps pass. Sampled timestamps are NOT a pass -- a spot-check can look clean while faces/bubbles are cut elsewhere in the same shot's zoom extremes.
- Transitions: hard cut default, fade 10%, slide/zoom/whip/glitch only on impact/reveal beats (glitch max 1 per 60s). No transition may cause a flicker. No effect may cover a face.
- SFX/music: plan SFX events, but render without SFX/music if files are missing (warning, never fail, never invent audio). Grade OFF by default.

VERIFY:
11. `ffprobe` the new video: duration, size, codec (h264 + AAC), shots, sentences. Must match the original's duration (±0.5s) unless you trimmed story (you must NOT trim story).
12. `video sync-audit` must PASS. Report wrong-picture %, per-minute wrong, worst 5s windows, tagged coverage per sentence, filler shots/seconds, min shot, and panels shown/total (info only). A `[filler_panel]` window means a filler failed the impact test — it is wrong-picture, not "story-correct filler": re-pair or let the tagged panel hold.
13. Spot-check the FINAL render at >=8 timestamps for: 0.5s glitch/freeze, pan-repeat swap, face cuts, colour blobs on faces, caption timing (on-time, present), caption bar (not covering art), system-window wide fill, triple-repeat stretch. View actual frames, do not guess from thumbnails. At minimum check: a system-window banner shot, a long (>5s) shot, a triple-repeat stretch if one existed, and the timestamps of any issue the original build had.
14. Compare vs original: table with duration, size, shots, sentences, sync %, drops (count + panel IDs + reasons, vs original), captions ON/OFF + coverage, transitions, SFX/music status. List what FIXED (glitch? pan repeat? face colour? caption timing? caption bar? triple repeat? P2 switch? system window?), what STILL BREAKS (unchanged from original), and any NEW issues (with timestamp + evidence).

REPORT:
15. Report ONLY facts you verified (ffprobe + sync-audit JSON + frames you viewed). Be honest: if something still breaks, say so. Do not claim perfection.
16. Deliver: test output path, duration, bytes, shots, sentences, sync-gate PASS/FAIL + wrong-picture %, drops list, captions status, SFX/music status, spot-check table (timestamp + result), old-vs-new comparison table, fixed / still-broken / new-issues lists.
```

## Prompt I — Build/continue a wide or adaptive-mode chapter video (NEW)

Prompt I is a docs-level prompt, like G and H: not in `agent_prompts.py`. Use
it for a `video/wide-mode-v2`-style build (or any multi-chapter continuation
session) instead of Prompt C, which assumes classic mode. The workflow
itself is canonical in `docs/CHAPTER_BUILD_WORKFLOW.md`; this entry is just
the trigger.

```
Build (or continue) the chapter video for chapter <N> of <SERIES>. My narration audio is at audio/chapter_<N>.<ext> in the session folder.
Read docs/CHAPTER_BUILD_WORKFLOW.md in full and follow it exactly, phase by phase. Re-read it again before starting any further chapter in this same session, even if you already built one a moment ago.
```

Or just invoke the `/build-chapter` skill with the series name, chapter
number(s), and audio file (if it isn't the default `audio/chapter_N.*`) —
it parses those and reads the workflow file for you, every time, so this
never depends on what's still "remembered" from earlier in a long session.

## After the agent finishes

- Check its final report against the real files (video exists? duration?
  sync gate passed?).
- You do the human-only steps: record audio, approve, publish, drop
  `music/bed.mp3` / `sfx/*.mp3` files (see `HYBRID_WAY.md`).
