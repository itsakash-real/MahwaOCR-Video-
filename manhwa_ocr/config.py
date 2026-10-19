"""Configuration loading. YAML file + environment variable overrides."""

from __future__ import annotations

import copy
import os
from dataclasses import dataclass, field, asdict
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
        "timeout": 180,
        "max_retries": 4,
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
    },
    "pipeline": {
        # Parallel vision calls. Keep modest to respect provider rate limits.
        "concurrency": 4,
        # Tiles per vision() call. 1 = one call per tile (the old behaviour).
        # Higher values cut round-trips; each tile is still cached separately.
        "vision_batch": 1,
        "cache": True,
        # Near-duplicate threshold for de-duplicating text across tile overlaps.
        "dedupe_threshold": 0.86,
        # Delete a chapter's downloaded pages and tiles once its transcript is
        # written. These are the bulk of disk use. Set false while iterating.
        "cleanup": True,
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


def load_config(path: str | Path | None = None, **overrides: Any) -> Config:
    """Load config from YAML, layering defaults -> file -> env -> kwargs."""
    # Deep copy: nested sections (video.panels, download.headers) must never be
    # shared with DEFAULT_CONFIG, or one run's overrides leak into the next.
    data = copy.deepcopy(DEFAULT_CONFIG)

    if path:
        p = Path(path).expanduser()
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
    return Config(**data)


def write_example_config(dest: str | Path) -> Path:
    p = Path(dest)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("w", encoding="utf-8") as fh:
        yaml.safe_dump(DEFAULT_CONFIG, fh, sort_keys=False, default_flow_style=False)
    return p
