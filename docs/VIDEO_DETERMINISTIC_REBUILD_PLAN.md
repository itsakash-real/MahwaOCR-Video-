# Video Build Deterministic Rebuild — Fix Plan

**Goal:** make `video build` produce Chapter-8-quality video *without* an AI agent, with zero audio drift, zero severed heads, zero bubble/watermark leaks, zero silently-skipped art, and uniform card shapes.

**Allowed crops after this work:** only `9:16 (0.5625)`, `3:4 (0.75)`, `1:1 (1.0)`, `4:3 (1.333)`, `16:9 (1.777)` — chosen deterministically per panel. No free-form `0.62–1.3` drift.

**Editing promise:** same visual language as `manhwa_recap_style_spec.md` (hard cuts, blur/black/white backgrounds, CapCut entrances, 2.6s eased motion then hold) — but every card now sits in a predictable grid, so the video looks intentionally edited.

---

## 1. Why the terminal path fails today (mapped to your Chapter 8 manual fix)

| Chapter-8 hand fix | What the CLI does today | Why it fails |
|---|---|---|
| **Step 1 — Audio audit + Whisper forced alignment (0.0s = "He stands before...")** | `video align` calls `sync.transcribe` then `sync.align` with difflib, then interpolates gaps. No intro-hook detection, no speech-end tail trim beyond `END_TAIL=1.5s`, and estimated timing on any whisper miss is silently accepted until `plan` blocks it. | Re-record with a missing/extra intro sentence misaligns every sentence after it by one. The Chapter-8 agent fixed it by hand-editing `cues.json`. |
| **Step 2 — Restored 15 silent art panels (P008-06, P010-05, …)** | `panels.py:is_text_only` + `align_engine:WINDOW=6` + `plan.py:even_units` all prefer art, but `art_score/crop_art` thresholds (`CROP_ART_BELOW=0.13`, `CROP_ART_WEAK=0.2`) mark low-texture art (hill, sky, courtyard) as `text_only`. `panels_sheet` labels them "TEXT — don't use" so later stages skip them. | Half the chapter disappears in `even` fallback and in any untagged stretch. Agent restored them by hand-picking IDs. |
| **Step 3 — Bubble/watermark cuts (top/bottom 15% + [x0,y0,x1,y1])** | `crops.text_bands` only looks at flat-white + hard lettering at the *absolute* top/bottom edge (`cap 0.45n`, `limit 0.06n`). Middle-edge bubbles (y0=0.22–0.38) and right-side bubbles survive. `_shave` only shaves 20% edges at 0.33 texty threshold. | Every shot needs a hand offset. |
| **Step 4 — Head/face audit (y0 0.08–0.18, never cutting forehead/hair)** | `crops.focal_crops` scores `soft*(1-white) -0.9*hard +1.2*skin` but has **no headroom constraint**. A tall 900×2500 panel with a face at y=0.14 centred behind a bubble will still lose the forehead when the best-scoring window sits lower. | `$y0>0.20` severs heads. Agent fixed each with a new y0. |
| **Step 5 — Plan assembly (61 shots, 0 gaps, valid enums)** | `plan.py:build_plan` accepts any `crop` fractions and any continuous `crop_aspect` in `0.62–1.6`. `validate` only checks `0≤x0<x1≤1` and `0.05` minimum size — not "is this one of 5 legal card shapes". | Cards render at 11+ different sizes → uneven masonry look you reported. |
| **Step 6 — Storyboard re-render + visual check** | `storyboard.write_storyboard` is correct, but `validate` + `build.make_plan` hard-fail on any `review_queue` items and on `sync=estimated`, so the honest terminal path is blocked and the user is told to use an agent/PROMPTS.md. | Deterministic path is intentionally gated. |

No single bug — the pipeline was *designed* to need the agent for aesthetic judgment. The fix is to move each judgment into deterministic code + a tight config.

---

## 2. Design principles for the rebuild

1. **Tags are the source of truth when they exist.** If `recaps/chapter_N.cues.json` exists, `video align` must produce a PASS that `video plan` trusts without a human. Lexical OCR evidence may *confirm* a tag, but may not silently replace it without strong evidence (≥2 real content words). Untagged sentences are allowed — they `HOLD_PREVIOUS` deterministically.
2. **Every crop is one of 5 card shapes.** `9:16`, `3:4`, `1:1`, `4:3`, `16:9`. Per layout we allow a *subset* (so the grid stays uniform). No continuous aspect drift.
3. **A face, once detected, is never cut.** Headroom is a hard constraint, not a scoring bonus.
4. **Bubbles/watermarks are detected in the top/bottom 15% strip, not just the absolute edge.** Standard scan watermarks (`ASURASCANS.COM`, credit banners) are excluded before scoring.
5. **Silent art is never dropped.** `text_only` becomes "do not *prefer*" not "do not *use* when tagged or when art coverage would otherwise gap".
6. **The review gate moves.** `video render` warns about `review_queue` but does not block `--force`-less renders when `sync=synced` and every `SHOW` segment is tag-backed. The old strict gate stays behind `--strict`.

---

## 3. The fix — 4 workstreams

### Workstream A — Audio ↔ script sync that cannot drift

**Problem today:** missing intro hook → every sentence off by one; TTS silence tail → duration mismatch; whisper miss → `estimated` timing that looks synced but isn't.

**Deterministic replacement for Step 1:**

- **A1. Script intake audit (new `video/cues.py: audit_cues`)** — before `video align`, compare `cues.json` sentence count vs `tts.txt` line count vs whisper word count. If `cues.json` sentence 1 has no whisper anchor within 2.0s and the audio duration is shorter than `words*2.7s` by >8s, emit `USER_ERROR: cues.json and audio disagree — did you record an intro not in the script? Re-run recap --finalize or re-record from tts.txt`. Do not interpolate silently.

- **A2. Whisper forced-alignment stays, but with deterministic fallback.** Keep `sync.align` (difflib over `tokens()`), but change `_timed_sentences` so:
  - if `sync_mode == "estimated"` and cues exist → re-try whisper with `small.en` hint before falling back (today it falls back once and stops).
  - `estimated` is surfaced as `sync: estimated` in `plan.json` and allowed to render with a warning, not a hard `UserError` — the warning tells the user the video will drift and how to fix it (install `pywhispercpp`, lower `whisper_model`). The old hard block moves to `video render --strict`.

- **A3. Duration truth.** Keep `END_TAIL` but compute `speech_end` from `timeline.json` words, not from FFmpeg silence detection, so a narrator who pauses mid-chapter doesn't truncate the video.

- **A4. No manual `P001-02: [0.0, 0.22, 1.0, 0.54]` re-discovery.** That belongs to Workstream C.

**CLI after:** `video align --chapter 8` either PASSes with `sync: synced, 0 gaps/overlaps` or fails loudly with an actionable message — never silently shifts every shot by one sentence.

---

### Workstream B — Never skip half the manhwa again

**Replace silent-panel loss (your P008-06 … P020-03):**

- **B1. Retune `is_text_only` for low-texture art.** Today `CROP_ART_BELOW=0.13`, `CROP_ART_WEAK=0.2` with `TEXT_ONLY_BELOW=0.25`. For the hill (P012-02), courtyard (P018-01), and silver-hair portrait (P016-05) the art is wide flat colour with soft shading — `soft mean` is low. Fix:
  - add **chroma score**: `art_score` already uses `sat>40` but `crop_art` does not weight it — make `crop_art` use `soft + 0.6*sat_texture` when picking the best window (one line in `crops.py:crop_art`).
  - lower the `text_only` boundary for panels that have `skin > 0.02` or `scene` non-empty (silent portraits have skin, landscapes have scene text from tile notes) — they must never be `text_only` regardless of texture.
  - keep credit-page rejection strict: only when `edge_pages & looks_like_credits(text)` — already correct.

- **B2. Alignment window always includes tagged panels.** Today `window_from(cursor)` returns `art_positions` only; a tagged silent panel that was marked `text_only` is outside the window so the tag is demoted to `tag_beyond_window_accepted` + `needs_review`. After B1, fewer silent panels are `text_only`, but add a safety net: if `tagged[0] in by_id`, inject it into `candidate_window` even if `text_only` (align already does `stored_window = [*window, selected]` — extend that to *candidates* so validation's `no_outside_interval_display` does not fire).

- **B3. Plan units: never inject a synthetic `first_art` when cues exist.** Today `synced_units` and `units_from_alignment` fall back to `first_art` when `ids` is empty, which can hide a deliberate `{same}`/`HOLD`. New rule: when alignment is the source (`alignment_units` path, which is now the only live path), an empty `ids` is `HOLD_PREVIOUS` by construction — do not synthesize a panel.

**Result:** `media/ch8/panels.json` will list 128 panels, ~10 `text_only` (true banners/credit pages), and every one of your 15 restored panels present as `text_only=false`.

---

### Workstream C — Bubbles, watermarks, and severed heads — deterministic

This replaces the per-shot hand `y0/y1` map you built (P001-02 … P019-05) with two passes inside `crops.py`.

#### C1. Bubble + watermark exclusion (replaces your "top/bottom 15% white_ratio >0.22")

New function `bubble_mask(img) -> np.ndarray`:

- Run at `ANALYSIS_W=160` (reuse `_maps`).
- Mark pixels where `white>0.5` or `hard>0.4` as candidate lettering.
- Connected-component or row-wise scan limited to `y in [0, 0.15] ∪ [0.85, 1.0]` and to `x in [0, 0.30] ∪ [0.70, 1.0]` (bubbles live on edges).
- Any run with `white_ratio > 0.22` spanning ≥6% of width is a **bubble band**. Record its `y0/y1`.
- Watermark lexicon: if `panel.text` contains `asura`, `scans`, `discord`, `patreon`, `kofi`, treat the bottom 12% as a watermark strip (your `P001-07`, `P002-07`).

**Applied in `clean_crop`:** before scoring, shrink the searchable vertical span from `[0,1]` to `[bubble_top, 1 - bubble_bottom]` where `bubble_top` is the bottom of the top bubble band + 1% padding, and `bubble_bottom` is the top of the bottom band. This is the deterministic version of every hand `[0,0.22,1,0.54]` you wrote.

#### C2. Head/face preservation (replaces your P016-05 … P019-05 forehead fixes)

New function `headroom_box(img, proposed_box) -> box`:

- Compute `_skin` mask at 120px scale (already exists).
- Find the topmost skin cluster whose area > 0.8% of panel and whose centroid is in the top 55% of the panel — that is the face.
- If no skin, fall back to `soft` density peak in top third (hair/eyes still texture).
- **Hard constraint:** the final crop's `y0` must be ≤ `face_top - 0.04` and `y1` ≥ `face_bottom + 0.04`. If the proposed best-scoring window violates it, slide the window up until it does (or expand to the next allowed aspect that can include it — see C3).
- Add a **chin/forehead margin test** in `validate`: reject any shot where `crop` cuts within 4% of a detected face's vertical extent (replaces the eye-check "severed heads y0>0.20").

Reuse the existing `skin` heuristic — no new model dependency — but add a single OpenCV Haar fallback (`cv2.CascadeClassifier` if `opencv-python` is installed, else skip) for the Renée portrait case where skin is pale and hair dominates. This keeps the deterministic path without requiring ML by default.

#### C3. Fixed 5-shape crop (replaces continuous `crop_aspect` 0.62–1.6 drift)

Define in `crops.py`:

```
ALLOWED  = [0.5625, 0.75, 1.0, 1.333, 1.777]   # 9:16, 3:4, 1:1, 4:3, 16:9
LAYOUT_ALLOWED = {
  "single": [0.75, 1.0, 1.333],   # portrait cards in 16:9 video — uniform look
  "pair":   [0.75, 1.0],           # narrow pair cards must be portrait
  "wide":   [1.777, 1.333],        # wide establishing shots
  "inset":  [0.75, 1.0],
}
SHORTS_ALLOWED = {
  "single": [0.5625, 0.75, 1.0],
  "pair":   [0.5625, 0.75],
  ...
}
```

Then `Designer.crop` becomes:

```
def crop(self, pid, layout):
  allowed = LAYOUT_ALLOWED[layout]  # or SHORTS_ALLOWED if fmt.portrait
  best = min(allowed, key=lambda a: _score_for_aspect(a))  # try each, keep headroom + bubble constraints
  return focal_crops(img, aspect=best, k=1, trim_text=True)[0] wrapped by _shave + headroom_box
```

**Card sizing determinism (fixes "uneven shapes and sizes"):** `frames.card_box` stays, but `fit(aspect, box)` now receives only one of 5 aspects per layout, so within a layout all cards share the same `cw/ch` bucket → the grid looks intentionally uniform. Keep the existing `fit()` geometry; the fix is at the call site (restrict `aspect`).

Add `plan.validate` rule: `crop` aspect must be within 0.02 of an entries in `ALLOWED` — otherwise error with `Rebuild with --force or pick one of 9:16, 3:4, 1:1, 4:3, 16:9`.

Storyboarding (`storyboard.write_storyboard`) already uses `ShotRenderer` — no change, but red `TEXT PANEL` boxes will now fire rarely because B1 keeps silent art.

---

### Workstream D — Plan assembly, validation, and the review gate

**Goal:** `video plan --chapter 8 --force` passes without touching `reviewed:true`.

- **D1. `align_engine.align_sentences` confidence:** promote `tag_confirmed_in_window` to `needs_review=False` (already) and also promote `no_evidence_default_sequence` to `False` when the selected panel is `art` and `window` length ≤3 (tight sequential stretch — there is nowhere else to go). This removes the 30+ false flags that today force `--force` on every render.

- **D2. `validate` stays 8 checks**, but `no_outside_interval_display` must not fail on the injected tagged-window fix in B2 (window already contains the tag).

- **D3. `build.make_plan` input hash** — today's `inputs` hash already includes the alignment hash (not cues). Keep it. Add `allowed_aspects` to the knob hash so switching to the 5-shape set invalidates old plans.

- **D4. Render gate:** move the hard `UserError` on `review_queue` and on `estimated` behind `allow_unreviewed` / `allow_estimated` flags, and add a new CLI flag `video render --strict` that restores today's blocking behaviour. Default `video build` after this work: warns, still renders when `sync=synced` and every `needs_review` segment is a `HOLD_PREVIOUS` or a sequential fallback in a dense stretch — the exact case that today blocks Chapter 8 for no reason. This is a one-line change in `build.render_chapter`.

- **D5. `pair` timing:** keep `pair_move_at` (second card arrival) logic — it already syncs the second card to `second_at` (its sentence start). No change.

---

## 4. New deterministic pipeline (what replaces the 6 hand steps)

```
audio/ch8.mp3 + recaps/ch8.cues.json
  │
  ├─ (A1) cue↔audio audit — fail fast on intro mismatch
  ├─ sync.transcribe (whisper base.en, cached in timeline.json)
  ├─ sync.align (difflib, numbered words spelled out, per-word timestamps)
  │
  ├─ (B) panels.json rebuild with B1 thresholds → all 128 panels, 15 silent art kept
  ├─ (C1+C2) bubble/watermark mask + headroom box per panel (inside crops.py)
  ├─ (C3) ALLOWED 5-shape pick per layout  → deterministic [x0,y0,x1,y1]
  │
  ├─ align_engine.align_sentences  (WINDOW=6, tag trust with B2 injection, D1 confidence)
  ├─ validate (8 checks) + review_queue (now ~0–2, not 31)
  ├─ build.make_plan  → plan.json with 60–65 shots, 0 gaps/overlaps, all crops in ALLOWED
  ├─ storyboard (3 sheets) — visual check, no red TEXT boxes on art
  └─ render (FFmpeg 1080p, hard cuts, 2.6s ease then hold, blur/black/white bg)
```

The only AI-built artefact that remains upstream is `recaps/chapter_N.cues.json` (panel tags). The video stack itself no longer needs per-sentence judgment.

---

## 5. Config changes (manhwa-ocr.yaml)

```yaml
video:
  allowed_aspects: [0.5625, 0.75, 1.0, 1.333, 1.777]  # locked set
  layout_aspects:
    single: [0.75, 1.0, 1.333]
    pair: [0.75, 1.0]
    wide: [1.777, 1.333]
    inset: [0.75, 1.0]
  bubble:
    edge_band: 0.15
    white_ratio: 0.22
    watermark_words: [asura, scans, discord, patreon, kofi]
  headroom:
    skin_min_area: 0.008
    margin: 0.04
    y0_max: 0.14          # replaces the old "y0 <=0.14" note in the agent prompt
    y1_min: 0.90
  strict_render: false    # when true, video render blocks on review_queue/estimated
```

No new required dependency. Optional `opencv-python` enables Haar face fallback; pipeline works without it.

---

## 6. Implementation order & effort (do one, ship, then the next)

| Phase | Files | What ships | Tested on |
|---|---|---|---|
| **P0 — Aspect lock** | `crops.py`, `plan.py:_Designer.crop`, `frames.py:crop_aspect`, `plan.validate`, `config.py:DEFAULT_CONFIG`, `tests/test_video.py` | `video plan` crops are always one of 5 shapes; uneven card bug fixed; no behaviour change on heads/bubbles yet | Synthetic panels + real ch8 panels_sheet diff |
| **P1 — Bubble/watermark strip** | `crops.py:bubble_mask`, `crops.text_bands`, `crops._shave` | Shots P001-02…P002-07 stop leaking bubbles without hand offsets; `chapter_8.storyboard` no bubble on borders | ch8 storyboard pixel check (top/bottom 15% white_ratio) |
| **P2 — Head preservation** | `crops.py:headroom_box`, `crops._maps/_skin`, optional `cv2` fallback | Renée (P016-05…P019-08) foreheads/eyes intact; no `y0>0.20` cuts | ch8 headroom regression: assert `y0 ≤ face_top-0.04` on portrait panels with skin |
| **P3 — Silent-art keep** | `crops.py:crop_art`, `panels.py:is_text_only`, `align_engine:window_from` | All 15 restored panels survive `panels.json` as `text_only=false`; `ch8` tagged recall 100% | `media/ch8/panels.json` count: 128 panels, ≤12 text_only, restored IDs present |
| **P4 — Sync audit + gate softening** | `sync.py`, `align_engine.py`, `build.py:render_chapter`, `cli.py:video render --strict` | `video build --chapter 8` runs end-to-end from terminal with `sync: synced`, `review_queue: 0–2` warnings, no agent | Real ch8 MP3: `python -m manhwa_ocr video build --chapter 8` PASS; duration ±0.1s, fps 30, no drift on intro hook |

Each phase is small enough to land behind one PR and keep `pytest -q` green. The ordering puts the visible "uneven sizes" fix first (your immediate complaint), then the aesthetic fixes, then the pipeline gate.

---

## 7. Acceptance checks (what "done" means)

- **Deterministic render:** `rm video/chapter_8.plan.json && python -m manhwa_ocr video build --chapter 8` produces a 1080p 30fps MP4 whose duration matches `audio/chapter_8.mp3 + 1.5s` within 0.1s, without any `reviewed:true` hand edit, on a clean checkout.
- **Zero severed heads:** every `shot.cards[].crop` with a detected face satisfies `y0 ≤ face_top - 0.04` and `y1 ≥ face_bottom + 0.04`. Chapter 8's 8 portrait close-ups pass.
- **Zero bubble leak:** no white speech-bubble pixels inside any shot's crop at the top/bottom 15% band (automated check: `text_share(crop) < 0.12` on storyboard mid-frames).
- **Zero skipped art:** `media/ch8/panels.json` contains all 15 restored IDs and they appear in `plan.json` shots.
- **Uniform grid:** every `shot.cards[].crop` aspect is within 0.02 of an entry in `ALLOWED`; per layout the actual rendered card `cw×ch` has at most 2 distinct sizes (not 11).
- **Audio lock:** re-recording ch8 with an extra/missing intro sentence fails `video align` with an actionable message instead of drifting every shot.

---

## 8. What stays agentic (and why that's fine)

- **Transcription (`run --next`) and recap tagging (`recap --prepare/--finalize`)** remain AI-heavy — reading stylised Korean/English lettering and writing tagged narration is genuinely cognitive work. The boundary is clean: once `cues.json` exists, the video stack is deterministic.
- **Optional storyboard review** stays as a non-blocking human glance (`video/ch8.storyboard_*.png` + `REVIEW.md`) — but it is a check, not a required fix step.

If you later want to remove even recap tagging from the agent, the next step is a separate project (vision model fine-tune on your house tag style) — not part of this video rebuild.

---

## 9. Risks & mitigations

- **Skin heuristic misses very pale / dark-skinned characters under heavy shadow.** Mitigated by the texture fallback (hair/eyes) and the optional Haar check; worst case the crop is still the 5-shape closest to centre — no worse than today — and `headroom_box` will just not constrain that panel.
- **Over-aggressive bubble shrink could crop art on panels where the bubble *is* the composition (e.g., a character shouting in foreground).** `clean_crop` scoring still penalises cutting art (`-0.6*soft -0.5*faces`) — the bubble strip is a *search window* shrink, not a crop itself; the best-scoring window inside that window still maximises faces/art.
- **Changing `is_text_only` thresholds could admit true caption pages as art.** Caption pages are still rejected when they are on `edge_pages` and `looks_like_credits`; and even if admitted, `plan.even_units` prefers art but will still show them if they are the only tagged panel — which is correct when the narrator is literally narrating the caption.

---

## 10. Immediate next action

1. Approve this plan (or adjust the 5 allowed aspects / layout subsets).
2. Land **P0 (aspect lock)** first — it is a 2-file change that fixes the "uneven sizes" look in one PR and is fully reversible.
3. Run the P0 storyboard diff on chapter 8 and confirm the 3:4 / 16:9 grid reads as intended before proceeding to P1.

*References: `manhwa-ocr.yaml`, `manhwa_ocr/video/crops.py`, `frames.py`, `plan.py`, `panels.py`, `align_engine.py`, `sync.py`, `build.py`, `docs/VIDEO_BUILD_PLAN.md`, your Chapter-8 hand-fix notes.*
