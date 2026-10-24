# Video workflow — research map + incident verdicts (2026-10-24)

Branch `research/video-workflow` (= `origin/video/wide-mode-v2` @ `1158c9e` merged with
`refine/recap-system` @ `fabd8e9`). Everything below was read from that code; every
claim carries `file:line`. This document is the Phase 1 (map) and Phase 2 (incident
verdict) deliverable of the video-workflow research mission. Phase 3 options live in
`docs/VIDEO_WORKFLOW_OPTIONS.md`; the fixes and their evidence live in the final report.

Note on lineage: the incident log was written against the production fork
("MahwaOCR-Video-", remote of this repo) whose placed-mode series
(`3d16aa8`..`1158c9e`) is merged into this branch. Where an incident describes code
that only ever existed on that lineage, the verdict says so.

---

## PHASE 1 — THE MAP

### 1. Stage graph

```
recaps/chapter_N.md ──(script.py finalize)──> recaps/chapter_N.cues.json + .tts.txt
                                                    │
audio (user mp3/wav, or video tts → Deepgram m4a)    │
                                                    ▼
media/chN pages ──(panels.py build_panels)──> media/chN/panels.json (+ panel images)
                                                    │
       .work/video/chN/vision_trim.json  (video vision-trim, Gemini; placed only)
                                                    │
                                                    ▼
pairing.py  write_task → pairs.json (agent edits) → apply → cues.json
                                                    │
                                                    ▼
align_engine.build_alignment ──> video/chapter_N.alignment.json (+ timeline.json whisper cache)
                                                    │
                                                    ▼
build.make_plan ──> video/chapter_N.plan.json (+ .qa.json + .verify.json + storyboard + REVIEW.md)
                                                    │
syncaudit.run ──> video/chapter_N.syncaudit.json  (the sync gate)
                                                    │
render.py render ──> .work/video/chN/segments/*.mp4 ──concat+audio──> video/chapter_N.mp4
                                                    (optional .ass captions burn pass)
```

One-shot drivers: `video build` (build.py:983 — align→plan→render per chapter),
`video pipeline` (pipeline.py:190 — panels→pair→align→plan→verify→sync-audit→render,
freshness-skipped, resumable, stop-on-failure).

### 2. Per-stage inputs / outputs / cache keys / invalidation

| Stage | Code | Inputs | Output | Cache key / freshness | Invalidated by |
|---|---|---|---|---|---|
| Panel build | `panels.py:build_panels` | page images + `video.panels` settings | `panels.json`, `media/chN/panels/` images | `fingerprint` over pages+settings (panels.py:509,562-564) | page file change, settings change, `--force` |
| TTS | `tts.py:synthesize_chapter` | cues.json sentences | `audio/chapter_N.m4a` + `.tts.json` | per-sentence `sha256(model\nspeed\ntext)` + WAV validity check (tts.py:219,222-227) | sentence text, model, speed, corrupt WAV, `--force` |
| tts-verify | `tts_verify.py` | m4a + tts.json | `.tts_verify.json` | whisper cache `verify_timeline.json` keyed (audio hash, model) | audio change |
| Vision trim | `gemini_vision.py:vision_trim_chapter` | panels + cues slice hints | `.work/video/chN/vision_trim.json` | none (re-run overwrites); never blocks — per-unit fallback (gemini_vision.py:327-415) | re-run |
| Pair task/apply | `pairing.py:write_task/apply` | cues.json + panels | `pairs.json` → cues.json | none | re-run |
| Align | `align_engine.py:build_alignment` | cues.json, panels.json, audio | `alignment.json` + `timeline.json` (whisper words) | timeline cache keyed `{audio sha256[:24], model, language}` (sync.py:153) — **cache hit never re-transcribes, `--force` is not a key** (sync.py:154-160) | audio bytes, whisper_model, whisper_language |
| Plan | `build.py:make_plan` → `plan.py:build_plan` | alignment.json, panels fingerprint, audio name/size/mtime, duration, format, knobs hash (min_hold, sync, mode-specific), fixes.json hash, slice_hints hash (if any), vision_trim hash (if any) (build.py:356-395,597-617) | `plan.json` + `.qa.json` + `.verify.json` + storyboard | `plan["inputs"]` equality; an edited/reviewed plan is never silently replaced (build.py:632-640) | any input hash change |
| Crop cache | `cropcache.py` via `crop_key` | fingerprint + panel geometry + shapes + options + face override | `.work/video/chN/crops/` | crop_key (verify.py:218, plan.py:779) | fingerprint/options change |
| Sync audit | `syncaudit.py:make_report` | plan.json, alignment.json, timeline.json, audio | `syncaudit.json` | none; render reuses a fresh (mtime ≥ plan) audit under same settings and re-evaluates the gate through the shared code path (build.py:919-944) | plan rebuild (mtime), settings change |
| Render | `render.py:render` | plan.json, panels, audio, sfx/music | `video/chapter_N.mp4` | per-shot segment cache: sha256 over RENDER_VERSION + shot spec + fmt + crf/preset + per-page stamps (render.py:147-163) | shot spec change (re-renders only that shot), page file change |

### 3. Where wall-clock goes

On this machine the timing log `Documents/ManhwaRecaps/<series>/.work/timing/` is the
record; `.work/video/chN/pipeline.json` carries per-stage seconds for pipeline runs.
Code-level cost facts:
- Whisper transcription dominates align on first run (model download + full decode; sync.py:165).
- Plan is the AI-free but image-heavy stage: `_panel_maps` + `pick_card` per panel (crops.py analysis at 160px width), LRU-cached via cropcache.
- Render draws every frame in Python (PIL) per shot and pipes raw RGB into one ffmpeg x264 process per segment, `-threads 2` per worker, `cpu_count` workers by default (render.py:219-231, ffmpeg.py:158-181). Segment cache makes one-shot fixes cheap (one shot re-renders).
- `video build` calls `make_plan` then `render_chapter` (build.py:983-1016); `video pipeline` adds freshness skips (pipeline.py:203-213).

### 4. Planner decision flow per mode (`plan.py:build_plan`, 2855-3132)

Common: units from alignment (`units_from_alignment` 225-299) → `hold_groups` (302-434, min_hold floor, cut budgets [start−lead, start+max_late]) → `_pair_up` (1039-1085) → `split_system_window_units` (574) → `_Designer.shot` (896-1036: layout by panel aspect/system-window, crop via fixes→cropcache→pick_card, deterministic background rotation `background_for_index` 627-646, motion no-repeat `motion` 865-882) → `split_long_shots` (>8s punch-in) → `ensure_split_crop_change` / `ensure_motion_variety` / `ensure_panel_diversity` → chapter cards/callouts/SFX/captions.

Mode passes, in build order (plan.py:3014-3030):
- **classic**: nothing extra. Cards at allowed shapes; caption bar bottom 8% (frames.py:125).
- **wide** (`_wide_pass` 1316): every card → `_wide_style_card` (1237): grow focal to figure (`extend_figure_range`), `wide_crop` (1105) window (min aspect 6/16, top-anchored, bubble-avoiding `safe_span`, caption-zone dodge 1196-1217, final `trim_blank_edges` revalidation 1224-1231); dead pans→zoom (1306-1310); risks recorded (caption_risk / figure_risk / reviewer_focal_ignored / unusable).
- **adaptive** (`_adaptive_pass` 1399): per-card wide treatment when panel aspect ≥ 1.333, else classic crop repositioned for figure.
- **panelfit** (`_panelfit_pass` 1950): measured-reference grammar; per-sentence SPAN motion sliced per shot; kinds fill/zoom/drift/traverse/duo (panelfit_fit 1631, panelfit_spec 1678); min_shot floor 0.3s replaces 3.0 (2889,2918); captions pinned off (2993-2996); split-flap title cards via `insert_title_cards` (2424).
- **fullframe** (`_fullframe_pass` 2797): per-card 16:9 camera path `fullframe_viewport` (2585): window + from/to rects; pan_y/pan_x/zoom routing; guardrail floors from `fullframe.max_upscale`.
- **placed** (`_placed_pass` 2276): per unit (panel or B1f slice band) edge-trim (`placed_trim_rect` 2200 — PURE-blank shave only, ≤40%/edge, no-art guard) or Gemini vision box (validated + ink-clamped, gemini_vision.py:280-306), then `placed_spec` (2181): maximum fit-inside, centred, `touches` recorded; all motion fields pinned 0; hints ignored+reported; hard cuts; title cards shared with panelfit.

QA gate (build.py `quality_report` 87-345): failures = disallowed card shape, panelfit/placed composition break, bubble-in-card, wide_unusable, shot <0.2s. Short shots (<min_hold) are counted, not fixed, and fail the SYNC gate instead.

### 5. Every place a value can silently default / fall back / go stale

1. **Config resolution**: `--config X` loads X over `DEFAULT_CONFIG` only — the repo's own `manhwa-ocr.yaml` is NOT in the chain (config.py:463-467 `find_config`, 497-518 `load_config`). Every key X omits silently takes the code default (e.g. `video.mode: classic` config.py:69 vs the repo yaml's setting).
2. **TTS config**: read only from `cfg.extra["tts"]` (tts.py:192-194); `DEFAULT_CONFIG` has no `tts` section → missing/partial `tts:` in the config in force silently means `aura-2-thalia-en` @ 1.23 (tts.py:50,55,195-197). Speed is sent to the API with no range/model validation (tts.py:111-112).
3. **Whisper fallback**: missing pywhispercpp or failed transcription → "estimated" timing with a loud `timing_warnings` entry (align_engine.py:561-597) — the run continues; render later refuses without `--force` (build.py:904-911).
4. **Whisper cache staleness**: `timeline.json` reuse keyed only on {audio hash, model, language} (sync.py:153); never re-transcribed under `--force`.
5. **Plan reuse**: cached plan reused only if `plan["inputs"]` matches and the plan wasn't hand-edited/reviewed (build.py:620-640) — safe; but `mode`-less classic knobs (e.g. `video.captions`) are not all in the hash (build.py:364-365 knob list).
6. **`_replace_dropped` substitution**: dropped tagged panel → nearest kept panel substituted in the PLAN only (build.py:51-80,717-721); cues.json/pairs.json keep the dropped tag → sync audit judges the original tag and fails (see incident 6).
7. **Vision trim fallback**: any Gemini failure → algorithmic trim, flagged, chapter never blocks (gemini_vision.py:352-361).
8. **Estimation inside align**: unmatched sentences interpolated between anchors (sync.py:383-397); long unmatched runs stop the build (align_engine.py:598-609).
9. **`sync.effective_settings`** warns (never errors) on missing config keys (sync.py:238-241).
10. **Render `--force`** = allow_estimated + allow_unreviewed + allow_sync_fail all at once (cli.py:1083-1085) — one flag both "rebuild" and "skip the gates".

### 6. What each gate measures — plan vs rendered frames

| Gate | Measures | On what |
|---|---|---|
| plan QA (quality_report) | shapes, composition invariants, bubbles, <0.2s shots, short-shot count, panels shown (info) | plan JSON (arithmetic on specs) |
| sync audit (syncaudit.audit 231-505) | wrong-picture share, worst 5s windows, late cuts, <floor shots, tagged coverage per sentence | plan JSON sampled at 0.5s vs alignment tags + cached whisper words — **not rendered frames** |
| render sync gate (build.py:912-963) | same numbers via cached report re-evaluated | plan JSON |
| verify (verify.py) | bubble/head/blank per panel, drops | panel pixels at plan time |
| storyboard | the real renderer's frame per shot | rendered pixels, but 480p-scale thumbnails; panelfit/placed tiles rendered at full format then halved (storyboard.py:87-93) |
| rendered-video QC | none automated today — `video review` uses a vision model on the STORYBOARD; nothing pixel-checks the final MP4 (this is the Phase-4 gap) |

---

## PHASE 2 — INCIDENT VERDICTS (all 23)

Ranking classes: **[A]** can silently ship a bad video, **[B]** wastes hours, **[C]** cosmetic/process. "Live" = reproducible on this branch as read.

| # | Incident | Root cause (file:line) | Status | Fix class / what exists | Regression test |
|---|---|---|---|---|---|
| 1 | `--config` series yaml ignores repo yaml (video.mode/min_hold silently default; shipped chapter rendered classic instead of wide) | `find_config` returns only the explicit path (config.py:463-467); `load_config` merges file over `DEFAULT_CONFIG` only (config.py:497-518) | **Live [A]** | Phase-4 fix 1: layer explicit config over program-folder config + effective-config echo + refuse ambiguous critical values | none → added |
| 2 | Chapters 1-11 narrated by thalia although orion configured | tts reads `cfg.extra["tts"]` only (tts.py:192-194); `DEFAULT_MODEL="aura-2-thalia-en"`, `DEFAULT_SPEED=1.23` (tts.py:50,55); `DEFAULT_CONFIG` has no `tts` section (config.py:13-361) — compounded by #1 | **Live [A]** | Phase-4 fix 1: `tts` becomes a first-class config section (defaults in `DEFAULT_CONFIG`), resolved model/speed/gap echoed and REFUSED when defaulted while a key file/env exists but no explicit model | partial (deepgram_speak URL pinned, test_deepgram_tts.py:97-121; the default-resolution path untested) → added |
| 3 | python without pywhispercpp → align silently estimated; render refuses hours later | `sync.available()` check (sync.py:44-49); fallback path warns with code+fix (align_engine.py:561-597, "P0 safety lock") but continues; render refusal names the code and fix (build.py:904-911) | **Mitigated [B]** — no longer silent, but the loop still burns a full plan+audit cycle before render refuses | Already-loud; Phase-4 fix 1 makes the effective whisper availability part of the build-start echo | warning path covered by test_p0_safety.py; availability-abort test added in fix 1 |
| 4 | pairs.json numbering ≠ cues numbering; editing key 76 changed the wrong sentence (intended 81) | pairs keys are cue-sentence numbers (pairing.py:216), alignment segments use the same numbering, but the AGENT assumed a different segmentation; `PAIRING.md` warns (pairing.py:254) and CHAPTER_BUILD_WORKFLOW.md documents the trap; sheets show `#n` + text (pairing.py:178-181) but pairs.json itself carries no text | **Live (tooling) [B]** | Phase-4 fix 3: pairs.json written WITH each sentence's text (numbered entries as objects is a format change — instead a `_sentences` note key + validation that keys match the text at apply time), and `pair --apply` refuses when the edited key's text on disk no longer matches what the editor saw | none → added |
| 5 | `{same}` hides its tagged panel; planner may reuse it as filler; sync gate fails | Mechanism at align_engine.py:171-179 (SAME → HOLD_PREVIOUS, selected_panel=None); **caught at align time** by `same_no_hidden_panel` (validate.py:140-155) with an actionable message; make_plan refuses non-PASS alignments (build.py:711-716) | **Fixed [B]** (wide-v2 hardening; the docs still carry the manual flip procedure as backup) | Gate change (align-time FAIL instead of late sync failure) | **yes** — test_p2_sync.py (`same_no_hidden_panel`) |
| 6 | Verify-drop substitution scores 0% at sync-audit unless BOTH pairs.json and cues.json re-paired by hand | `_replace_dropped` substitutes in the plan only and logs a "re-pair it" hint (build.py:51-80,717-721); audit judges the ORIGINAL tag (`_expected_sets` syncaudit.py:120-136 reads `tagged_panels` from alignment) | **Live [A]** | Phase-4 fix 3: one-command official substitution — `video pair --apply` learns `"_substitutions"` written by make_plan (dropped→shown per sentence) and rewrites cues.json+alignment expectations together; audit credits a substitution recorded in the plan | none → added |
| 7 | Shouting face + dominant bubble → grey-oval mask; only cure drop+re-pair, must be caught BEFORE render | verify drops mask-risk panels with no clean crop at PLAN time (verify.py:312-320); painting removed entirely (`clean_bubbles` no-op — AGENT_WAY.md rule 6, crops.py:1177-1191 patch_visibility legacy note); drop + substitution visible in the plan log (build.py:669-676) | **Fixed at plan time [A→B]**; residual is #6 (the re-pair half) | Drop rule + logged substitution; #6 fix completes it | mask-risk drop path exercised in test_verify_fixes_review.py; substitution path test added with fix 3 |
| 8 | Floating panels (classic small centred cards) | Classic card geometry (frames.py:128-139 `card_box` ≤0.62W, `slot_centres` 0.46H) — by design of classic mode | **By design**; superseded by placed mode (placed_fit plan.py:2160: maximum fit-inside, `touches` top_bottom/left_right; QA failure `placed_bad` build.py:242-245 rejects any card not at exact maximum fit) | Presentation spec, not a bug | test_placed.py pins composition |
| 9 | Scrolling panels (panelfit traverses/drifts) rejected | panelfit B1e traverse/drift (plan.py:1749-1759, 1762-1773) — by design of panelfit mode | **By design**; superseded by placed mode (motion fields pinned zero, plan.validate rejects a moving placed spec plan.py:3329-3340) | Presentation spec | test_placed.py pins zero-motion |
| 10 | White bands BAKED INSIDE panels; delivery QC measured the FRAME, white sat in the FOREGROUND; v1 trimmer only blank rows; placed pilot shipped white margins | v1 trim = light-gutter rows only (trim.py:39-151); v2 = ink-bbox + gap slicing at panel build (trim.py:233-348), opt-in `video.panels.trim_v2` (config.py:245-246); placed pass adds per-unit `placed_trim_rect` PURE-blank shave (plan.py:2200-2273) + Gemini vision boxes clamped to ink bounds (gemini_vision.py:302-306; 1158c9e fixed the over-trim: only pure blank, bubble bands are content); frame QC `qc_frames` judges white-space/floating/bands (gemini_vision.py:533-584) but is a library helper, NOT wired into any build gate | **Fixed at prep [A→B]**; rendered-frame white-share gate still missing → Phase-4 fix 4 | prep trim + vision trim artifact (hash-invalidates plans, build.py:612-617) | test_trim2.py, test_placed.py, test_gemini_vision.py; rendered-frame gate test added in fix 4 |
| 11 | pan_down end leaks ~0.035-0.04 below the plan window | Card pan viewport: travel = 85% of half-slack with zoom ≥1 (frames.py:479 `sx, sy = (sw - vw) / 2 * 0.85`; zoom map 467), source = the crop region only (frames.py:369-370,406) → the visible rect is mathematically confined INSIDE the crop; the 0.85 clamp predates the repo's verified baseline (git log -S, commit e10adbb) | **Not reproducible in current code [hypothesis: fixed during wide-mode-v2 crop hardening (d2e581d era)]** — marked hypothesis because the ch7 artifact predates this lineage | Pan-inside-window invariant | none → added (cheap pin: pan viewport ⊆ crop at p∈{0,0.5,1}) |
| 12 | Caption-band collisions; whole-panel cards (aspect ≥ 0.667) ignore reviewer focals so bottom text sits in the band structurally | Wide/adaptive: `wide_crop` whole-panel branch returns the whole panel and never runs the caption-zone collision check (plan.py:1132-1152 — only `edge_text` checked); windowed branch does dodge+flag (plan.py:1196-1217); reviewer focal discarded on whole panels, flagged `wide_reviewer_focal_ignored` (plan.py:1290-1296). Placed: `video.captions` default True (config.py — no `captions` key in DEFAULT video → vcfg.get("captions", True) plan.py:2992) and placed does NOT pin captions off (only panelfit does, plan.py:2993-2996) → burned captions overlay a panel that by rule touches the bottom wall | **Live [A]** (wide/adaptive whole-panel band; placed+captions-on) | Phase-4 fix 4: rendered-frame caption-band occupancy check + plan-time warning for placed with captions on; pin `placed.captions: false` default (mirror panelfit) | none → added |
| 13 | Silent re-fits: focal grown past cap re-fit/re-anchored; wide windows aspect<0.667 height-center-cropped by renderer (720px floor) | Re-fits are flagged now: `wide_figure_risk` / `wide_reviewer_focal_ignored` (plan.py:1170-1175,1290-1296; QA build.py:291-293); renderer floor (frames.py:399 `max(0.375W, …)`) can only bind when aspect <0.667, which `wide_crop` upstream forbids (plan.py:1163-1164); panelfit over-cap panels reported `panelfit_over_cap` | **Fixed (flagged, non-silent) [C]** | Flags + upstream invariant | test_wide.py / test_panelfit.py cover the flags |
| 14 | Dead-pan false alarms: plan metric flagged visibly-moving shots (frame diffs 8.5-41); motion assignments shuffle between plans | Plan metrics computed from specs: wide swaps dead pans to zooms at plan time (plan.py:1306-1310); panelfit `static` computed from actual spec motion (plan.py:2076-2102); fullframe `static` = frm==to (plan.py:2733). Metrics are per-plan by construction (each build_plan recomputes) | **Fixed by construction [C]**; "counts on an earlier plan" is inherent — always re-read the current plan's QA | Metric honesty | test_panelfit.py (static report), test_fullframe.py |
| 15 | Fractions measured on storyboard composites understated by the scale factor; tiles render ~0.03-0.04 tighter at top than the plan window | Storyboard tiles are drawn by the SAME renderer at preview format (proportionally exact; storyboard.py:46,78,103-105); panelfit/placed tiles render at FULL format then downscale halves (storyboard.py:87-93). The measurement trap was review-process (measuring on sheets) — the docs now mandate "measure from the actual panel pixels, never from thumbnails" (AGENT_WAY Prompt C) | **Fixed (process + architecture) [C]** — no code bias found; proportional rendering makes sheet-fraction math exact up to resize | Doc rule | none needed (no code defect); fix 4's frame QC also removes the need to measure by hand |
| 16 | Background refresh every ~2s (historical) | Deterministic rotation `background_for_index` i%10: 4→black, 9→white, else blur (plan.py:627-646); continuous motion, `ensure_split_crop_change`, `ensure_motion_variety`, `ensure_panel_diversity` | **Fixed [C]** | Standing rule implemented | **yes** — tests/test_background_rotation.py |
| 17 | Filler avalanche (46% filler incl. 0.49s shots) | Old place_coverage deleted; `place_impactful` (align_engine.py:329-518) enforces: ≤8s = tagged only; >8s split tagged 65% + ONE impact-tested filler; cap ceil(dur/3); MIN_SHOT 3.0 (align_engine.py:80-87); sync gate fails any <floor shot (syncaudit.py:413-416), any sentence under tagged-coverage threshold (syncaudit.py:421-436), any impact-failing filler counts wrong-picture (syncaudit.py:275-281) | **Fixed [A→guarded]** | Pacing rules enforced at THREE layers (placement, plan QA count, sync gate) | **yes** — tests/test_impactful_selection.py |
| 18 | `video pipeline --force` not a true cascade: plan hardcoded force=False; whisper cache never re-transcribes | Plan half FIXED: `_plan` passes `force=ctx.force` (pipeline.py:100,214) — pinned by test_pipeline.py:155. Whisper half LIVE: `_align` takes no force (pipeline.py:90-94) and `sync.transcribe` returns cache on any key match (sync.py:153-160) | **Half-live [B]** | Phase-4 fix 2: force propagates into `build_alignment(force=...)` → `transcribe(force=True)` bypasses the cache read (still writes it back) | plan half yes (test_pipeline.py:155); whisper half none → added |
| 19 | Gate-report truncation: tooling kept last 4 lines, "2 flagged" vs real 26 | Program writes FULL reports to files (syncaudit.json syncaudit.py:688; tts_verify.json tts_verify.py:105; qa.json build.py:754) and prints full summaries; the truncation was agent-side shell piping | **Not a code defect [C]** — tooling; Phase-4 fix 4 additionally writes the rendered-frame QC report to a file and the docs now name the files as the source of truth | Tooling/doc | n/a; fix 4's report file covers the new gate |
| 20 | `[LOOP: …]` bracket swallowing the next sentence leaked a \x00 pseudo-sentence into cues | Interleaved-marker parser (cues.py:199-213) strips \x00 tokens before text derivation (cues.py:235-236); `_protect_bracket_cues` masks terminators inside bracket spans so a cue can never split (cues.py:175-183); `_CUE_LEAK_RE` makes a surviving literal cue a hard recap error (cues.py:311,371-378) | **Fixed [A]** (via the recap-system merge, cf20ae6) | Parser hardening + hard error | **yes** — tests/test_recap_refine.py (LOOP/cue-leak cases) |
| 21 | Splitter broke on interior ?/! inside quotes; {same} didn't count toward tagged_ratio | `_protect_quoted_interior` + `_protect_quoted_continuation` (cues.py:128-165); honest tagged count counts same-only as tagged (cues.py:277-283) | **Fixed [A]** (recap-system merge, cf20ae6) | Parser hardening | **yes** — tests/test_recap_refine.py |
| 22 | tts_lint hard gate would refuse em-dash house style; speed sent unvalidated (Aura-1 gets Aura-2 speed); corrupt WAV cache poisoned builds; missing BT.709 | Em-dash: LIVE — `_DASH_PAUSE` matches a spaced em/en dash and tts REFUSES the chapter (tts_lint.py:23-24,64-70; tts.py:199-201). Speed: LIVE — `&speed=` always in URL, no range/model check (tts.py:111-112,197). Corrupt WAV: FIXED (`_is_valid_wav`, tts.py:145-151,222-227). BT.709: FIXED (ffmpeg.py:170-176 explicit tags) | **2 live [B], 2 fixed** | Phase-4: dash-pause demoted to WARNING when the dash is an em-dash (house style) while `--` stays an error; speed validated (0.7-1.5 for aura-2, omitted for other models) and echoed | corrupt-WAV + URL tests exist (test_deepgram_tts.py); BT.709 untested → color-tag test added |
| 23 | Two renders must never target one output (player caches by filename); /tmp 512MB tmpfs; endscreen/watch-next files still generated though the series rule is no intro/outro | Output collision: LIVE — render writes `chapter_N.part.mp4` then `os.replace` (render.py:239,310); no lock/refusal for a concurrent render on the same output. /tmp: the program never uses tempdir for scratch (util.py:121-157 atomic writes sit next to targets; segments under `.work/` render.py:48-52) — that half is an ops constraint of the production box. Endscreen/watch-next: LIVE — written unconditionally on every plan (build.py:759-766,1121-1137) | **Live [A for collisions, C for metadata]** | Phase-4 fix 4/5: render refuses to start when the final output already exists and is newer than a running marker (lock file with PID), and endscreen generation becomes opt-in (`video.endscreen: true` / `--endscreen`) | none → added |

### Incident → fix ranking (what Phase 4 must address)

- **[A] can silently ship a bad video**: #1 (config chain), #2 (voice default), #6 (drop substitution), #10 residual (rendered white-space gate), #12 (caption band on whole panels/placed), #23 (output collision).
- **[B] wastes hours**: #3 residual (echo), #4 (pairs numbering), #18 whisper half, #22 dash/speed.
- **[C] cosmetic/process**: #13, #14, #15, #19, #23-metadata.

---

## PHASE 4 EVIDENCE — real chapter built with the fixed workflow (2026-10-24)

Series `a-regressor-s-tale-of-cultivation` ch1, placed mode, all numbers from
files on disk (session folder `Documents/ManhwaRecaps/a-regressor-s-tale-of-cultivation/`).

- TTS: 46 sentences, 4902 chars, 234.1s, voice `aura-2-odysseus-en` speed 1.3 gap 0.11s
  (echoed by the new effective-config printout); `audio/chapter_1.tts_verify.json`: 0 flagged.
- The incident-5 guard fired on a REAL chapter: sentence 13's `{same}` hid P003-02
  across a story jump — caught at align validation (validate.py:140-155), fixed by
  re-tagging through pairs.json (`pair --apply` also flipped `same`).
- The frame-QC gate caught the incident-10 class on the first render (38 white-band
  hits); frame-level inspection (S001/S003/S019/S026 extracts) refined the semantics:
  bands carrying art (SFX overflow, diagonal borders, logo canvas, foam) are CONTENT
  per the 1158c9e trim ruling — notes, not failures. Strict failures are reserved for
  units whose bounds prep could not guarantee (trim fallback / capped shave). Final
  report: `video/chapter_1.frameqc.json` — pass, 44/44 shots, 0 failures, 49 notes.
- Sync gate: PASS — strict_match 1.0, wrong_picture 0.0s of 468 judged samples,
  0 late cuts, 0 short shots, timeline fresh (whisper base.en).
- ffprobe of `video/chapter_1.mp4`: h264 High, **1920x1080 @ 30 fps**, yuv420p
  **bt709/tv** tagged, AAC 48000 Hz stereo, duration 3:54.11 (= audio duration 234.11s),
  11,634,385 bytes.
- Per-stage timing (`.work/video/ch1/pipeline.json`, final run): panels skipped,
  pair 5.9s, align 0.1s (cached timeline; fresh transcription 45s on first run),
  plan 3.8s, verify 0.0s, sync-audit 0.1s, render 85s with all 44 segments cached
  (cold render 932s at 1920x1080; QC decode included), qc skipped (fresh) — total 92s.
  The historical 1-3h/chapter claim from the incident log was not reproducible on
  this machine (different hardware); the measured cold-build cost here is dominated
  by render (932s) + first-run whisper (45s).
- Full test suite: 1161 passed, 1 skipped, 0 failed (includes 26 incident tests in
  `tests/test_video_incidents.py`, one per fixed incident).
