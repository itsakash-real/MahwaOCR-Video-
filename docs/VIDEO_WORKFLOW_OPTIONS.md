# Video workflow — Phase 3 options research (2026-10-24)

Options tables for the worst areas, with a recommendation each. Context that
constrains every choice: the production box is 2 vCPU; frame drawing is Python/PIL
(render.py:92-97); the repo deliberately avoids vendor SDKs and heavy deps
(sync.py:10-13 documents why faster-whisper/MKL was rejected); a per-shot segment
cache already exists (render.py:147-163).

Sources are linked inline; the general findings come from web research done
2026-10-24 (ffmpeg parallelism, forced-alignment tooling, broadcast QC practice,
pydantic-settings practice).

---

## A. Render architecture

| Option | Correctness | Speed on 2 vCPU | Complexity | Notes |
|---|---|---|---|---|
| **Keep segment-parallel CPU render (current)** | identical-output guarantee per shot; frame-exact joins (plan.shot_frame_bounds); one-shot fix re-renders one segment | bottleneck is PIL frame drawing, not x264; workers = cores; static-shot freeze already skips re-draws | already built | render.py:79-103,213-231 |
| Single-pass filtergraph (zoompan/crop chain) | can't express the card/panelfit/placed compositing (alpha layers, shadows, duo cells) without rewriting frames.py in filter language; caption pass already re-encodes once | one ffmpeg process, no Python draw → likely faster per frame but only if compositing were expressible | very high (rewrite) | rejected: the compositor IS the product |
| NVENC / VAAPI hardware encode | same pixels (rate control differs slightly) | encode offload only; drawing stays CPU → little gain unless GPU present; 2 vCPU box likely has none | medium (encoder selection, quality parity: `-rc` tuning, test needed) | worth a config knob (`video.hwaccel: nvenc|vaapi|off`) only when a GPU box is available |
| More segment workers + x264 thread cap tuning | unchanged | scales with cores (already `-threads 2`/worker, ffmpeg.py:169) | trivial | the right first move on any bigger machine |
| Segment cache keyed by (panel, window, mode, caption) | already effectively this: key = whole shot spec + fmt + crf/preset + page stamps; captions excluded by design so caption edits never re-render shots (render.py:147-163,286-309) | one-shot fixes are already one-segment re-renders | already built | no change needed |

**Recommendation: keep the current architecture.** It already has the property the
mission asks for (a one-shot fix re-renders one shot). Add only: (1) a rendered-output
collision lock (incident 23), (2) an optional `video.hwaccel` encoder switch left off
by default, (3) per-stage timing already lands in `.work/video/chN/pipeline.json` —
surface it in the build log. Segment-parallel with CPU x264 is also what the
ffmpeg community converges on for CPU-bound compositing (linear chains fastest;
parallelism via processes — [MPEGFlow](https://www.mpegflow.com/topics/encoding/ffmpeg-filter-complex-patterns),
[Super User discussion](https://superuser.com/questions/1218945/ffmpeg-multiple-outputs-performance-single-instance-vs-multiple-instances));
NVENC helps only when filtering is not the bottleneck ([r/linux thread](https://www.reddit.com/r/linux/comments/18gry7o/)).

## B. Sync verification (forced alignment)

| Option | Word timestamps quality | Deps / weight | Risk | Notes |
|---|---|---|---|---|
| **whisper.cpp (pywhispercpp) + difflib (current)** | base.en/small.en word stamps are adequate because the script is KNOWN — alignment only needs anchors (sync.py:300-348) | ~150-300 MB model, no torch | MKL-class failures avoided (ggml) | already cached per (audio hash, model, language) |
| [stable-ts](https://github.com/jianfch/stable-ts) `align()` | best-in-class regrouping/refinement | **torch** (+ model) | heavy dep on a 2 vCPU box; the deliberate MKL-avoidance rationale applies to torch CPU builds too | [comparison context](https://arxiv.org/html/2406.19363v1) |
| [ctc-forced-aligner (MMS)](https://huggingface.co/MahmoudAshurf/mms-300m-1130-forced-aligner) | excellent multilingual, known long-audio drift reports ([SO](https://stackoverflow.com/questions/79875656/)) | torch + transformers | drift on long chapters is exactly our failure mode | attractive only if narration goes multilingual |
| [WhisperX](https://github.com/m-bain/whisperX) / [easyaligner](https://www.reddit.com/r/MachineLearning/comments/1soyqfw/easyaligner_forced_alignment_with_gpu) | batched + wav2vec2 refinement; GPU-oriented | torch, GPU to be worth it | no GPU here | — |
| [torchaudio Wav2Vec2 CTC](https://docs.pytorch.org/audio/stable/tutorials/forced_alignment_tutorial.html) | precise | torch | DIY maintenance | — |

**Recommendation: keep whisper.cpp; change WHAT is verified, not the aligner.**
The gap the incidents expose is that the gate of record reads the PLAN, not the
RENDER. Add a post-render sync check: decode the final MP4's audio stream and
re-run `sentence_anchors` against it; compare each sentence start to the plan's shot
boundary (tolerance max_late + 0.1s). That catches mux/concat drift and proves the
delivered file, costs one whisper pass over already-cached words (same cache key →
free when the audio is byte-identical to the input).

## C. Visual QC automation on rendered frames

The user's three rejections + caption band, and where each is checkable:

| Check | Pixel metric on rendered frames | Gemini/VLM | Verdict |
|---|---|---|---|
| Floating panel (not touching walls) | exact: the plan's placed spec already proves max-fit (QA `placed_bad`); on frames, near-white/uniform margin bands at frame edges outside the foreground rect | weaker: judgment, non-deterministic, cost/frame | **pixels** |
| White space INSIDE foreground | exact: sample the foreground rect (known from the spec) → near-white row/col share at its edges (same math as `placed_trim_rect` plan.py:2252-2266, run on the rendered crop) | catches "looks empty" better than strict near-white (off-white cream) but not deterministically | **pixels primary, VLM optional** |
| Scrolling/motion where none allowed | exact for placed: a placed shot's frames must be byte-identical (first==last frame hash, mid-frame sample diff = 0 modulo codec noise ~<0.5% mean abs) | unnecessary | **pixels** |
| Caption-band collision | exact: re-render the caption region mask (the .ass layout is deterministic) and measure panel-ink coverage under it; simpler proxy: ink share in the bottom band of the frame that isn't caption text | readability of covered text is a judgment call | **pixels for occupancy, VLM for readability** |
| Wrong picture for the sentence | not a pixel question | VLM (or the existing pairing sheets) | **VLM/agent** |

Broadcast QC practice is exactly this hybrid: deterministic per-frame checks for
structure, sampling + models for semantics
([MovieLabs QC vocabulary](https://movielabs.com/md/qcvocabulary/video),
[Telestream](https://www.telestream.net/vidchecker/overview.htm),
[NVIDIA VLM sampling guide](https://developer.nvidia.com/blog/vision-language-model-prompt-engineering-guide-for-image-and-video-understanding)).
The repo already ships the VLM half as a library (`gemini_vision.qc_frames`,
gemini_vision.py:533-584 — unused by any gate today).

**Recommendation:** a new `video qc` gate (wired into `video pipeline` after render
and into `video render` as a report): decode ~3 frames per shot (start/mid/end) with
the bundled ffmpeg, run the four pixel checks above, write
`video/chapter_N.frameqc.json` (FULL report — also answers incident 19's
truncation class), fail on floating/white-space/motion violations; Gemini QC behind
`video.frameqc.vision: true` (off by default; keys already exist).

## D. Planner design (constraint-based shot planning)

| Option | Correctness | Speed | Complexity | Notes |
|---|---|---|---|---|
| **Constructive planner + invariant validation + gates (current)** | the pacing rules are enforced constructively (place_impactful caps, hold_groups floor, cut budgets); validate() rejects spec violations pre-render for placed/panelfit/fullframe | fast (greedy, no solver) | already built | the residual defects are in MEASUREMENT (plan vs frames), not in scheduling |
| Full CP-SAT / OR-Tools scheduler (shots as intervals, coverage constraints) | optimal-ish; could natively encode "tagged coverage ≥80%" and beat greedy on dense narration | solver startup ~100ms-1s/chapter — fine; but modeling the mode-specific composition constraints doubles the code | high; new dep | gains are marginal: the sync gate already FAILS any violation, and short_shots are left honest by design |
| Constraint checks as plan.validate() rules (middle ground) | every user-locked rule becomes a mechanical pre-render rejection | negligible | low | mostly exists (plan.py:3138-3397); extend with caption-band + placed captions pin |

**Recommendation: keep the constructive planner, extend `validate()`/QA with the two
missing invariants (placed mode must pin captions off like panelfit does; a
whole-panel wide card with low text must carry caption_risk), and make the
rendered-frame QC (section C) the gate of record. A solver buys optimality the
channel's style doesn't need; the incident trail shows the failures were
measurement gaps, not scheduling optima.

## E. Config safety

| Option | Catches incident 1/2/3? | Deps | Fit with repo |
|---|---|---|---|
| **Layered resolution + effective-config echo + refuse-defaulted criticals (hand-rolled)** | yes: `--config` layers OVER the program-folder yaml; every build prints resolved mode/voice/speed/gap/min_hold/whisper; a critical value that would be silently defaulted refuses the run | none | matches the stdlib-yaml, no-SDK philosophy |
| [pydantic-settings](https://pydantic.dev/docs/validation/latest/concepts/pydantic_settings) with `extra="forbid"` + `model_dump()` echo | yes, plus type errors at load | pydantic v2 | heavy new dep for a config that is 3 sections deep; `extra=forbid` would also reject forward-compatible configs the repo deliberately tolerates (config.py:372-377) |
| jsonschema validation | shape only; silent-default semantics still need the layering fix | jsonschema | half a solution |

**Recommendation: hand-rolled (fix 1).** The repo's `Config.extra` design
(config.py:372-377) intentionally keeps unknown sections so a config shared between
checkouts never crashes — pydantic's `forbid` would break that contract. Echo +
refuse-on-default gives incidents 1-3 the same fail-fast property without the dep
(pydantic practice reference: [fail-fast + effective-config printout](https://python.plainenglish.io/managing-application-configuration-in-python-with-pydantic-settings-c8c8694620c8)).

---

## What Phase 4 implements (only what Phases 1-3 condemn)

1. **Config safety (incidents 1, 2, 3)** — layered config, `tts` first-class, effective-config echo, refuse-defaulted voice/mode on video builds.
2. **True --force cascade (incident 18)** — `video pipeline/build --force` re-transcribes (bypasses the timeline cache read) and re-plans.
3. **Pairing safety (incidents 4, 6)** — pairs.json carries `_sentences` text; apply refuses stale-numbered edits; make_plan records substitutions; `video pair --apply` consumes them into cues.json so the audit judges reality.
4. **Rendered-frame QC gate (incidents 10, 12, 15, 19, 23)** — per-shot pixel checks on the rendered MP4 (floating / white-in-foreground / motion / caption band), full JSON report, render-output collision lock, endscreen generation opt-in.
5. **TTS validation (incident 22)** — speed validated + model-family aware (omitted for non-aura-2), em-dash demoted to warning for the house style; BT.709 regression test.

Plus one regression test per incident fixed, named after it.
