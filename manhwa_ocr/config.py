"""Configuration loading. YAML file + environment variable overrides."""

from __future__ import annotations

import copy
import os
from dataclasses import dataclass, field, asdict, fields
from pathlib import Path
from typing import Any, Dict

import yaml

DEFAULT_CONFIG: Dict[str, Any] = {
    "series": {
        "name": "untitled-series",
        "workdir": "./out",
    },
    "provider": {
        # one of: anthropic | gemini | openai | ollama
        # "openai" works for anything OpenAI-compatible: OpenRouter, LM Studio,
        # vLLM, Groq, together.ai, local proxies.
        "kind": "anthropic",
        "vision_model": "claude-haiku-4-5-20251001",
        # Text-only model for chapter synthesis + memory updates (cheaper).
        # Leave null to reuse vision_model.
        "text_model": None,
        "base_url": None,
        "api_key_env": "ANTHROPIC_API_KEY",
        "temperature": 0.0,
        "max_tokens": 4096,
        # Assembly and recap calls return a whole chapter of text; truncated
        # output is unparseable, so they get a much larger budget.
        "text_max_tokens": 16000,
        # Fast-fail transport: hung connections are detected early and the
        # key pool rotates / trips to the agent fallback instead of grinding.
        "timeout": 90,
        "max_retries": 3,
        # Tile payload shaping (images.encode_image): tiles are downscaled and
        # JPEG-compressed IN MEMORY before base64 -- a ~4-11MB PNG tile lands
        # at ~180-350KB without changing what's on disk.
        "image_max_width": 800,
        "image_quality": 85,
        # Consecutive key failures before a key is disabled (circuit breaker).
        "circuit_breaker_threshold": 3,
    },
    "slicing": {
        # Tall webtoon strips must be cut up or the model sees a thumbnail.
        # This is the single most important quality setting.
        "enabled": True,
        "target_height": 1400,
        "overlap": 180,
        # Snap cuts to low-detail "gutter" rows so speech bubbles aren't bisected.
        "snap_to_gutter": True,
        "gutter_search": 220,
        # Images taller than this get sliced. Shorter ones pass through whole.
        "min_height_to_slice": 1800,
        # Downscale very wide pages before slicing (keeps tiles in model's sweet spot).
        "max_width": 1100,
        # Pages wider than tall (reader-site credits banners, e.g. AsuraScans'
        # first page) are skipped for reading; the page file itself is kept.
        "drop_landscape_pages": True,
    },
    "download": {
        "timeout": 60,
        "max_retries": 3,
        "headers": {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"
            ),
        },
        # Politeness delay between page downloads, seconds.
        "delay": 0.25,
    },
    "video": {
        "format": "16:9",           # 16:9 (1920x1080) or 9:16 (1080x1920, Shorts)
        "fps": 30,
        # None = not chosen in any config layer: every reader resolves it to
        # "classic", but the build-start echo marks it DEFAULTED (incident 1:
        # a shipped chapter rendered in classic mode when wide was intended,
        # because --config never read the repo yaml that set it).
        "mode": None,               # classic | wide | adaptive | panelfit | fullframe | placed
                                    #   wide: every card full-height, min 6/16 wide
                                    #   adaptive: per shot, a naturally wide/landscape panel gets
                                    #   the wide treatment; a narrower one keeps its classic
                                    #   crop/shape but without the equal-area shrink (no more
                                    #   stale small cards) -- real per-panel variety in one video
                                    #   panelfit: the MEASURED reference style (see
                                    #   reference-video-measured-spec.md) -- every panel
                                    #   shown WHOLE at its native aspect over its own
                                    #   full-brightness blurred fill, gentle LINEAR
                                    #   drift or constant-rate zoom-in, wide panels
                                    #   full-bleed, NO burned captions, NO shake/flash,
                                    #   split-flap title cards on timeline cues
                                    #   (see panelfit: below)
                                    #   fullframe: no cards at all -- every shot is a full-bleed
                                    #   16:9 camera viewport travelling over the panel
                                    #   (pan through tall windows, zoom inside wide ones)
                                    #   placed: the Panel Placement Rule -- every panel
                                    #   shown WHOLE at native aspect, centred and scaled
                                    #   to the MAXIMUM fit inside the frame (tall/square
                                    #   panels touch top+bottom, wider-than-16:9 panels
                                    #   touch left+right) over its own full-brightness
                                    #   blurred fill, with NO motion at all (no drift,
                                    #   traverse, pan or zoom; motion hints ignored)
                                    #   and hard cuts only (see placed: below)
        "wide": {
            "min_width_ratio": 0.375,  # 6/16 of the frame wide; no thin slivers, ever
            "max_upscale": 1.5,         # a panel is never upscaled past this (1080p sharpness)
        },
        "adaptive": {
            "wide_threshold": 1.333,   # a panel's own aspect >= this (4:3) earns the wide treatment
        },
        "panelfit": {
            # Every value below is pinned to the MEASURED reference video
            # (reference-video-measured-spec.md, 85 shots). Where the older
            # text descriptions disagreed with the measurements, the
            # measurements won (user arbitration, 2026-10-23).
            "bg_blur": 50,             # Gaussian radius (px at 1080p) of the cover-scaled,
                                       # blurred copy of the panel that fills the frame
            "bg_opacity": 1.0,         # that background layer's opacity over black:
                                       # 1.0 = FULL brightness, no dimming (measured;
                                       # the text specs' 50-60% was wrong)
            "shadow_dy": 20,           # B1e: the foreground's drop shadow — black,
            "shadow_blur": 10,         # offset (0, +20px), blur 10px, opacity 40%,
            "shadow_opacity": 0.40,    # layered between backdrop and panel (the
                                       # backdrop itself stays at full brightness)
            "fill_aspect": 1.6,        # panel aspect (w/h) at/above this fills the frame
                                       # full-bleed; narrower panels are height-fit pillars
            "drift_aspect_max": 0.72,  # pillars narrower than this (w/h) get a gentle
                                       # vertical drift; wider pillars get a zoom-in
            "drift_speed": 0.0667,     # drift speed, frame-heights/second — the
                                       # invariant (B1d F2): 72 px/s at 1080p
                                       # exactly (3.75% of frame WIDTH/s at 16:9;
                                       # measured band 3.2-4.3% width/s); travel
                                       # is derived
            "drift_min_travel": 0.05,  # a drift SPAN's travel floor, share of frame
                                       # height (measured totals: 5-13%); motion
                                       # still runs at drift_speed throughout
            "drift_max_travel": 0.13,  # ... and never more than this share; at the
                                       # cap the panel HOLDS (never reverses)
            "drift_headroom": 0.03,    # extra foreground oversize beyond the travel,
                                       # so a drift never exposes a panel edge
            "zoom_rate": 0.05,         # zoom-in rate: +5.0%/s (measured band
                                       # +3.6-6.5%/s) -- a constant RATE, so the total
                                       # grows with shot length; never a fixed total,
                                       # never a zoom-out
            "zoom_rate_impact": 0.088, # impact shots zoom at +8.8%/s instead (measured
                                       # on the reference's weapon-clash shot). There is
                                       # NO shake and NO white flash in this mode --
                                       # the reference uses neither, on any shot.
            "zoom_out_fast_rate": 0.176, # B1e: zoom-OUT rate for a blast, used
                                       # ONLY by the per-sentence motion hint
                                       # "zoom_out_fast" (the user's detonator ->
                                       # explosion ruling): exactly 2x the impact
                                       # zoom-in rate. Default routing never zooms
                                       # out; a hint is the only way in.
                                       # B1f: the "zoom_in" hint needs no rate of
                                       # its own — it forces the zoom geometry at
                                       # "zoom_rate" above, on panels that would
                                       # otherwise drift or traverse.
            "zoom_max_total": 0.45,    # total zoom cap: at +45% the scale holds
                                       # (measured totals top out at +46%)
            "max_upscale": 2.4,        # the foreground is never upscaled past this
                                       # (readability floor for small panels)
            "still_limit": 0.5,        # seconds: nothing on screen may stay still longer
                                       # while its span still has travel/scale left
                                       # (motion is continuous by construction; at
                                       # the measured travel/zoom caps the position
                                       # holds, as the reference's own zooms hold)
            "min_shot": 0.3,           # this mode's shot floor (measured reference
                                       # minimum 0.30s; classic keeps its own 3.0s floor)
            "split_above": 3.0,        # sentences longer than this split into two
                                       # shots (the reference's median shot is ~2s)
            "split_min_piece": 1.2,    # smallest piece such a split may produce
            "captions": False,         # NO burned-in captions in this mode (measured:
                                       # the reference carries none -- platform CC only)
            "title_cards": True,       # Title Card Generator: timeline cues in the
                                       # narration ("Age 4" / "Week 6") become split-flap
                                       # cards (see video/titlecards.py)
            "tts_gap_seconds": 0.11,   # the mode's recommended narration gap. The
                                       # reference's MEASURED median gap is 0.214s,
                                       # but a 0.20 setting rendered as 0.296s median
                                       # (per-sentence TTS edge padding ≈ +0.09s), so
                                       # 0.11 lands the measured gap at ≈0.20s (B1d F4).
                                       # The global TTS default stays 0.35; use this
                                       # value for tts.gap_seconds for this mode.
        },
        "placed": {
            # The Panel Placement Rule (user, 2026-10-24): panels are
            # PLACED, never floated, never scrolled. The fit is the
            # exact maximum fit-inside (contain) scale -- deliberately
            # NO max_upscale cap here: a cap would leave a small panel
            # smaller than the maximum, i.e. floating, which is the
            # one thing this mode exists to eliminate.
            "bg_blur": 50,             # Gaussian radius (px at 1080p) of the cover-scaled,
                                       # blurred copy of the panel that fills the frame
                                       # (shared with panelfit: same machinery, same value)
            "bg_opacity": 1.0,         # that background layer's opacity over black:
                                       # 1.0 = FULL brightness, no dimming, never
                                       # white/black/empty bands
            "shadow_dy": 20,           # the foreground's drop shadow — black,
            "shadow_blur": 10,         # offset (0, +20px), blur 10px, opacity 40%,
            "shadow_opacity": 0.40,    # layered between backdrop and panel; the
                                       # backdrop itself stays at full brightness);
                                       # shared with panelfit's treatment
            "title_cards": True,       # timeline cues become split-flap title
                                       # cards, via the same planner machinery
                                       # panelfit uses (insert_title_cards; the
                                       # master audio is never cut) — timing
                                       # otherwise follows the classic planner
            # Incident 12: a placed panel touches the BOTTOM wall by rule, so
            # burned-in captions can only sit ON the art. Pin them OFF like
            # panelfit does; a series that wants captions sets
            # video.captions + placed.captions: true deliberately.
            "captions": False,
        },
        "gemini_vision": {
            # Gemini Vision prep/QC (video/gemini_vision.py): `video
            # vision-trim` asks Gemini for each placed unit's art box,
            # sanity-clamped against the algorithmic ink trim, and the
            # QC helper judges rendered frames against the placement
            # rule. The model and pacing are pinned here (mirrored by
            # the module's GEMINI_* constants and tests). Key FILES are
            # named — never a key value: keys are read from these files
            # at call time and rotated across calls, exactly like the
            # Deepgram key-file pattern, and never logged or stored.
            "model": "gemini-3.5-flash",  # live flash on the keys (2.5-flash 404s for new users)
            "batch_size": 6,              # labelled images per call (storyboard grouping)
            "max_attempts": 6,            # bounded total attempts per call, across keys
            "key_files": ["~/.config/manhwa/gemini-1.key",
                          "~/.config/manhwa/gemini-2.key",
                          "~/.config/manhwa/gemini-3.key"],
        },
        "fullframe": {
            "max_upscale": 2.4,        # zoom guardrail: a viewport's source is never smaller
                                       # than the frame / this (1080/2.4 = a 450px source-height
                                       # floor at 900px-wide panels); keeps full-bleed upscaling
                                       # from cratering on deep punch-ins
            "zoom_depth": 1.18,        # target push_in/pull_out depth when the window has room
            "pan_travel": 1.0,         # a pan's window holds this many extra viewport lengths
                                       # of travel beyond one viewport (1.0 = window is 2 viewports)
        },
        "crf": 20,                  # x264 quality: lower = better and bigger
        "preset": "veryfast",
        "lead": 0.15,               # show a panel this long before its sentence starts
        "min_shot": 1.0,            # recap pacing: ~2.7s average, 1-5s range
        "max_shot": 5.0,
        "target_shot": 2.7,
        "pair_ratio": 0.3,          # share of even-timed shots shown as two cards
        "workers": 0,               # parallel shot renders; 0 = half the CPU cores
        "panels": {
            "gutter_min": 24,       # px of blank rows that separate two panels
            "flat_threshold": 4.0,  # how uniform a row must be to count as blank
            "gutter_drift": 8.0,    # max brightness change along a gutter (rejects gradients)
            "drop_below": 80,       # px; smaller fragments are dropped
            "merge_below": 220,     # px; small fragments merge into a neighbour
            "skip_landscape": True, # skip wide pages (scanlation credit banners)
            "tighten_edges": True,  # trim white/black gutter rows off a panel's top and bottom
            "white_threshold": 242, # gray above this across a row = white gutter
            "black_threshold": 13,  # gray below this across a row = near-black gutter
            "trim_v1": False,       # Build 1: gutter-colour blank-row trim + review flags (opt-in)
            "trim_cap": 0.40,       # max share of a panel's height the v1 trim cuts per side
            "trim_v2": False,       # Rule 6: full ink-bbox trim (white+black, rows+cols) +
                                    # white/black-gap slicing, at panel build (opt-in; panelfit prep)
        },
        # Bubble/text detector (Build 4/3 shared step): ogkalu RT-DETR, Apache-2.0.
        # "model": path to the ONNX file ("" = ~/.config/manhwa/models/ or $MANHWA_DETECTOR_MODEL)
        "detector": {
            "model": "",
            "threshold": 0.35,      # min box score kept (the model separates cleanly: junk < 0.05)
        },
        # Card framing. Cards keep the panel's full width and pick a vertical window.
        # Head detection beyond skin colour, for white/silver hair and gray skin.
        "heads": {
            "pale": True,           # try the pale/gray head detector when skin colour finds none
            "keep_top": 0.25,       # figure but no head: a card never starts this far down (0 = off)
        },
        "crop": {
            "full_width": True,     # false = the old behavior (zoomed, side-cropped windows)
            "min_width_kept": 0.9,  # last-resort bubble avoidance may narrow a card, never below this
        },
        # Recap style is calm: only a gentle shake on {impact} lines by default.
        "effects": {"shake": True, "punch": False, "flash": False, "end_fade": True},
        "music": None,              # path to a music file, relative to the series folder.
                                 # None still means: use music/bed.mp3 in the series folder when
                                 # it exists (P3 safe default), else render music-free.
        "music_volume": 0.22,     # 15-25% under narration (sidechain ducks further under speech)
        "music_fade": 0.5,        # music fade in/out, seconds (>= 0.3s)
        "sfx_dir": None,          # folder (relative to the series folder) with whoosh.mp3,
                                 # impact.mp3, ding.mp3, sting.mp3. None = no SFX, never fails.
        # Narration sync and hold limits. These are the numbers `video plan` and
        # `video sync-audit` actually use; sync.effective_settings() reads them
        # from here (video.min_hold and video.sync.*) or warns by name.
        #   min_hold:        no picture stays up for less than this (seconds).
        #   max_late:        a cut may start at most this long after its sentence starts.
        #   lead:            hard early bound - a cut may lead its sentence by at most this.
        #   min_segment:     anti-collapse guard for sentence starts only (NOT a shot
        #                    floor — raising it would pull panels ahead of their words).
        #   max_hold:        a single picture held longer than this gets split.
        #   max_unmatched:   a run of whisper-unmatched speech longer than this stops the build.
        #   override_evidence: OCR overlap above which a tag/OCR disagreement is flagged (tag still shows).
        #   gate_wrong:      most wrong-picture time (another sentence's panel on screen,
        #                    or a filler that fails the impact test) allowed overall.
        #   gate_window:     most wrong-picture time allowed in any five-second window.
        #   gate_grace:      extra cut lateness tolerated before the gate fails.
        "min_hold": 3.0,
        "sync": {
            "min_segment": 0.25, "max_late": 0.3, "lead": 0.2,
            "max_hold": 8.0, "max_unmatched": 6.0, "override_evidence": 20,
            "gate_wrong": 0.05, "gate_window": 0.25, "gate_grace": 0.2,
        },
        # Recording text. spell_numbers: the NEW recaps/chapter_N.tts.txt (and the spoken text in
        # cues.json) write digits as words (99 -> ninety nine). Existing files with audio are never touched.
        "tts": {"spell_numbers": True},
        # `video intro`: the chapter-0 trailer built from panels of several chapters.
        # min_hold: shortest time a picture stays up. pair_ratio: share of neighbouring panels shown
        # two at a time. darken: darker grade (0-0.6). backdrop: always the blurred panel, never black bars.
        "intro": {"min_hold": 2.0, "pair_ratio": 0.5, "darken": 0.15, "backdrop": "blur",
                  "beat_effects": ["shake", "flash", "fade_out"]},
        # `video hook` / `video previously`: 30-second auto-picked videos.
        # target_seconds is clamped into [min_seconds, max_seconds]; audio
        # longer than the cap (or shorter than the floor) logs a warning.
        # seed makes the auto-pick's near-tie jitter reproducible.
        "hook": {"target_seconds": 30.0, "min_seconds": 20.0, "max_seconds": 45.0, "seed": 7},
        "whisper_model": "small.en",
        "whisper_language": None,   # e.g. "hi" for Hindi narration (use a multilingual model)
        # Incident 23: end-screen metadata (chapter_N.endscreen.json +
        # .watch_next.txt) is written only when a series opts in. The current
        # series rule is no intro/outro pieces at all; `video endscreen`
        # still writes them on demand.
        "endscreen": False,
    },
    "pipeline": {
        # Parallel vision calls across rotated keys (keep within provider RPM).
        "concurrency": 6,
        # Tiles per vision() call. 1 = one call per tile (the old behaviour).
        # Higher values cut round-trips; each tile is still cached separately.
        "vision_batch": 4,
        # Agent mode: maximum tiles per shard task. prepare() splits every
        # chapter into ceil(tiles / this) shard tasks BY DEFAULT, because one
        # agent reading a whole chapter of ~100+ tile images in a single
        # context is the known context-blowup failure. Shards are sized from
        # this bound, not hardcoded. 10 keeps each shard small enough for the
        # hybrid fallback to parallelize quickly.
        "agent_shard_tiles": 10,
        # Hybrid fallback: provider failures / missing keys shift the
        # UNRESOLVED tiles into agent-mode TASK_k.md shards (completed tiles
        # are preserved in tile_results.json) instead of crashing the run.
        "hybrid_agent_fallback": True,
        # The instruction printed in TASK.md: how many shard workers the
        # driving agent should run at once.
        "max_parallel_shards": 6,
        # Provider mode watchdog: if stage-1 tile reading makes no progress
        # for this many minutes, the run fails loudly (cached tiles are kept)
        # instead of hanging for hours.
        "stall_minutes": 5,
        "cache": True,
        # Near-duplicate threshold for de-duplicating text across tile overlaps.
        "dedupe_threshold": 0.86,
        # Delete a chapter's downloaded pages and tiles once its transcript is
        # written. These are the bulk of disk use. Set false while iterating.
        "cleanup": True,
    },
    # Narration synthesis (`video tts`). `model` deliberately has NO default
    # here: a build must never silently fall back to a code-pinned voice
    # (incident 2). synthesis refuses with a fix-it message until the config
    # in force names one. speed applies to aura-2 models only (0.7-1.5) and
    # is OMITTED from the API call for any other model (incident 22).
    "tts": {
        "model": None,
        "speed": None,
        "gap_seconds": 0.35,
        "key_file": None,
    },
    "recap": {
        # Narration voice for recap writing ("default", "speedrun", "powerfantasy"
        # or "manhwabro").
        # "default": calm present-tense voice telling a friend why the chapter matters.
        # "speedrun": Speedrun Gossip voice — breathless past-tense gossip, role
        #   labels instead of names, chain-of-consequences sentences with So/But/
        #   only-to pivots, an editorial aside every few sentences. Full spec:
        #   docs/RECAP_STYLE_SPEEDRUN.md
        # "powerfantasy": the Power Fantasy system — in-medias-res hook with the
        #   "This guy is..." formula, lore drop after the flex, "our guy" lexicon,
        #   hype-man reactions, gamified vocabulary. A range of chapters is
        #   recapped only once EVERY transcript in the range exists, so the
        #   writer holds the whole story. Full spec:
        #   docs/RECAP_STYLE_POWERFANTASY.md
        # "manhwabro": sarcastic hype-man fan narrator — cold open in the fight,
        #   a "So, why is he so strong?" pivot into the lore, "our boy / this
        #   brat / bro" for the protagonist, roasts for everyone else, meme
        #   slang. Writes from the FULL transcript (OCR + character and panel
        #   descriptions). Full spec: docs/RECAP_STYLE_MANHWABRO.md
        "style": "default",
        # Owner's standing length rule (pinned; the quality report warns
        # outside this band): 3-5 minutes of speech per chapter.
        "min_words": 560,
        "max_words": 920,
    },
}


@dataclass
class Config:
    series: Dict[str, Any] = field(default_factory=dict)
    provider: Dict[str, Any] = field(default_factory=dict)
    slicing: Dict[str, Any] = field(default_factory=dict)
    download: Dict[str, Any] = field(default_factory=dict)
    pipeline: Dict[str, Any] = field(default_factory=dict)
    video: Dict[str, Any] = field(default_factory=dict)
    recap: Dict[str, Any] = field(default_factory=dict)
    # Narration TTS (Deepgram Aura). First-class so a config that omits it
    # can be DETECTED and refused at synthesis time (incident 2: chapters
    # 1-11 shipped narrated by the code-default voice although another
    # voice was configured in the repo yaml, which --config never read).
    tts: Dict[str, Any] = field(default_factory=dict)
    # Top-level YAML sections this build doesn't know about (e.g. a section
    # written by a newer build's config). Kept, not dropped, so a config
    # file shared between checkouts never crashes load_config -- only code
    # that actually reads a given section needs to know about it.
    extra: Dict[str, Any] = field(default_factory=dict)

    @property
    def workdir(self) -> Path:
        return Path(self.series.get("workdir", "./out")).expanduser().resolve()

    @property
    def series_name(self) -> str:
        return self.series.get("name", "untitled-series")

    def api_key(self) -> str | None:
        env = self.provider.get("api_key_env")
        return os.environ.get(env) if env else None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def _deep_merge(base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
    out = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        elif value is not None:
            out[key] = value
    return out


# What each backend expects when the user hasn't said otherwise.
PROVIDER_DEFAULTS: Dict[str, Dict[str, Any]] = {
    "anthropic": {"api_key_env": "ANTHROPIC_API_KEY", "vision_model": "claude-haiku-4-5-20251001"},
    "gemini": {"api_key_env": "GEMINI_API_KEY", "vision_model": "gemini-2.5-flash"},
    "openai": {"api_key_env": "OPENAI_API_KEY", "vision_model": "gpt-4o-mini"},
    "ollama": {"api_key_env": None, "vision_model": "llama3.2-vision"},
}
_ALIASES = {"claude": "anthropic", "google": "gemini", "openrouter": "openai", "openai-compatible": "openai"}
_KEY_OWNER = {
    "ANTHROPIC_API_KEY": "anthropic",
    "GEMINI_API_KEY": "gemini",
    "GOOGLE_API_KEY": "gemini",
    "OPENAI_API_KEY": "openai",
}
CONFIG_FILENAME = "manhwa-ocr.yaml"


def _family(kind: str | None) -> str:
    kind = (kind or "anthropic").lower()
    return _ALIASES.get(kind, kind)


def _fit_provider_to_kind(provider: Dict[str, Any]) -> None:
    """Never send one vendor's secret, or model name, to another vendor.

    A config written for Claude, then run with `--provider gemini`, would
    otherwise post ANTHROPIC_API_KEY to Google along with a Claude model id.
    Keys and models that are recognisably another provider's are swapped for
    this provider's defaults; anything custom the user set is left alone.
    """
    family = _family(provider.get("kind"))
    defaults = PROVIDER_DEFAULTS.get(family)
    if defaults is None:
        return

    key_env = provider.get("api_key_env")
    owner = _KEY_OWNER.get(str(key_env)) if key_env else None
    if not key_env or (owner and owner != family):
        provider["api_key_env"] = defaults["api_key_env"]

    other_defaults = {d["vision_model"] for f, d in PROVIDER_DEFAULTS.items() if f != family}
    for field_name in ("vision_model", "text_model"):
        model = provider.get(field_name)
        foreign = model in other_defaults or (
            family in {"gemini", "ollama"} and str(model or "").startswith(("claude", "gpt-"))
        )
        if field_name == "vision_model" and (not model or foreign):
            provider[field_name] = defaults["vision_model"]
        elif field_name == "text_model" and foreign:
            provider[field_name] = None


# The program folder's own manhwa-ocr.yaml (present for a `pip install -e .`
# checkout), used when the current folder has none, so commands behave the same
# from anywhere.
PROGRAM_CONFIG = Path(__file__).resolve().parents[1] / CONFIG_FILENAME


def find_config(explicit: str | Path | None = None) -> Path | None:
    """--config, then $MANHWA_CONFIG, then manhwa-ocr.yaml in the current folder,
    then the one in the program folder."""
    if explicit:
        return Path(explicit).expanduser()
    env = os.environ.get("MANHWA_CONFIG")
    if env:
        return Path(env).expanduser()
    local = Path.cwd() / CONFIG_FILENAME
    if local.is_file():
        return local
    return PROGRAM_CONFIG if PROGRAM_CONFIG.is_file() else None


def output_folder(config_path: str | Path | None = None) -> Path | None:
    """`output_folder:` from the config: where all series folders live.

    A relative path is relative to the config file, so the program folder and
    the output folder can be moved together without editing anything.
    """
    path = find_config(config_path)
    if not path or not Path(path).is_file():
        return None
    try:
        with Path(path).open("r", encoding="utf-8-sig") as fh:
            value = (yaml.safe_load(fh) or {}).get("output_folder")
    except (OSError, yaml.YAMLError, AttributeError):
        return None
    if not isinstance(value, str) or not value.strip():
        return None
    folder = Path(os.path.expandvars(value.strip())).expanduser()
    return (folder if folder.is_absolute() else Path(path).resolve().parent / folder).resolve()


def resolve_config_chain(explicit: str | Path | None) -> "list[Path]":
    """The config layers an explicit ``--config`` inherits (incident 1):
    the program folder's yaml, then the current folder's — each only when it
    exists and is a different file. Empty when there is no explicit config,
    and empty when the explicit path IS the ``MANHWA_CONFIG`` pin (that env
    var is an isolation override: hermetic tests pin it so the developer's
    live yaml never leaks into them)."""
    if not explicit:
        return []
    ex = Path(explicit).expanduser()
    env_cfg = os.environ.get("MANHWA_CONFIG")
    if env_cfg:
        try:
            if Path(env_cfg).expanduser().resolve() == ex.resolve():
                return []
        except OSError:
            pass
    out: "list[Path]" = []
    for base in (PROGRAM_CONFIG, Path.cwd() / CONFIG_FILENAME):
        try:
            if base.is_file() and base.resolve() != ex.resolve() \
                    and base.resolve() not in {b.resolve() for b in out}:
                out.append(base)
        except OSError:
            pass
    return out


def load_config(path: str | Path | None = None, *, base_layers: "list[Path] | None" = None,
                **overrides: Any) -> Config:
    """Load config from YAML, layering defaults -> base_layers -> file -> env -> kwargs.

    ``base_layers`` is how an explicit ``--config <series>.yaml`` inherits the
    working/program yaml's keys (incident 1: a series config used to silently
    drop the repo yaml's ``video.mode`` / ``min_hold`` / ``tts`` back to code
    defaults — a shipped chapter rendered in classic mode when wide was
    intended). ``resolve_config_chain()`` builds the list; only the CLI's
    ``--config`` passes one, so every other loader keeps single-file
    semantics (and ``MANHWA_CONFIG`` stays an isolation override for tests).
    """
    # Deep copy: nested sections (video.panels, download.headers) must never be
    # shared with DEFAULT_CONFIG, or one run's overrides leak into the next.
    data = copy.deepcopy(DEFAULT_CONFIG)

    def _merge_file(p: Path) -> None:
        nonlocal data
        if not p.exists():
            raise FileNotFoundError(f"Config file not found: {p}")
        try:
            with p.open("r", encoding="utf-8-sig") as fh:
                loaded = yaml.safe_load(fh) or {}
        except yaml.YAMLError as exc:
            from .util import UserError

            raise UserError(f"{p} is not valid YAML: {exc}") from None
        if not isinstance(loaded, dict):
            from .util import UserError

            raise UserError(f"{p} should contain sections like 'provider:' and 'slicing:'.")
        data = _deep_merge(data, {k: v for k, v in loaded.items() if isinstance(v, dict)})

    for _base in (base_layers or []):
        _merge_file(Path(_base).expanduser())
    if path:
        _merge_file(Path(path).expanduser())

    env_map = {
        ("provider", "kind"): "MANHWA_PROVIDER",
        ("provider", "vision_model"): "MANHWA_VISION_MODEL",
        ("provider", "text_model"): "MANHWA_TEXT_MODEL",
        ("provider", "base_url"): "MANHWA_BASE_URL",
        ("provider", "api_key_env"): "MANHWA_API_KEY_ENV",
        ("series", "workdir"): "MANHWA_WORKDIR",
        ("series", "name"): "MANHWA_SERIES",
    }
    for (section, key), env_name in env_map.items():
        val = os.environ.get(env_name)
        if val:
            data[section][key] = val

    for dotted, value in overrides.items():
        if value is None:
            continue
        section, _, key = dotted.partition("__")
        if key and section in data:
            data[section][key] = value

    _fit_provider_to_kind(data["provider"])
    known = {f.name for f in fields(Config)} - {"extra"}
    extra = {k: v for k, v in data.items() if k not in known}
    return Config(**{k: v for k, v in data.items() if k in known}, extra=extra)


def write_example_config(dest: str | Path) -> Path:
    p = Path(dest)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("w", encoding="utf-8") as fh:
        yaml.safe_dump(DEFAULT_CONFIG, fh, sort_keys=False, default_flow_style=False)
    return p


def effective_video_settings(cfg: "Config | Dict[str, Any]") -> Dict[str, Any]:
    """The resolved values a video build actually runs with (incidents 1-3):
    every build-start echo prints these, so a defaulted mode/voice/whisper
    model is seen BEFORE hours of work, not after a wrong render.

    Keys whose resolution fell back to a code default are reported with
    ``defaulted: True`` so the echo can mark them (``video tts`` refuses a
    defaulted voice outright — see tts.resolve_voice_and_speed).
    """
    vcfg = (cfg.video if hasattr(cfg, "video") else cfg.get("video")) or {}
    tcfg = (cfg.tts if hasattr(cfg, "tts") else cfg.get("tts")) or {}
    if not tcfg and hasattr(cfg, "extra"):
        tcfg = (getattr(cfg, "extra", None) or {}).get("tts") or {}
    mode = vcfg.get("mode")
    whisper = vcfg.get("whisper_model")
    return {
        "mode": {"value": mode or "classic", "defaulted": mode is None},
        "format": vcfg.get("format", "16:9"),
        "min_hold": vcfg.get("min_hold", 3.0),
        "fps": int(vcfg.get("fps", 30)),
        "whisper_model": {"value": whisper or "small.en", "defaulted": whisper is None},
        "tts_model": {"value": tcfg.get("model"), "defaulted": not tcfg.get("model")},
        "tts_speed": tcfg.get("speed"),
        "tts_gap_seconds": tcfg.get("gap_seconds", 0.35),
        "captions": vcfg.get("captions", True),
        "endscreen": bool(vcfg.get("endscreen", False)),
    }


def format_effective_video(cfg: "Config | Dict[str, Any]") -> str:
    """One-line-per-setting echo block for build logs."""
    s = effective_video_settings(cfg)

    def mark(entry: Any) -> str:
        if isinstance(entry, dict) and entry.get("defaulted"):
            return f"{entry['value']}  [DEFAULTED - not set in any config; set it explicitly]"
        return entry["value"] if isinstance(entry, dict) else entry

    return ("effective video config: mode={mode} format={format} min_hold={min_hold:g}s "
            "fps={fps} whisper={whisper_model} captions={captions} endscreen={endscreen}\n"
            "effective tts config:    voice={tts_model} speed={tts_speed} gap={tts_gap_seconds:g}s"
            ).format(**{k: mark(v) for k, v in s.items()})
