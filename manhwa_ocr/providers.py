"""Vision/text model backends.

Everything speaks plain HTTP via `requests`, so there are no vendor SDK
dependencies to keep in sync. Four kinds cover essentially every runner:

  anthropic  -> Claude API
  gemini     -> Google Gemini native generateContent API
  openai     -> any OpenAI-compatible /chat/completions endpoint. That includes
                OpenAI, OpenRouter, Groq, Together, LM Studio, vLLM, llama.cpp
                server, and most local proxies.
  ollama     -> native Ollama /api/chat (llava, llama3.2-vision, qwen2-vl, ...)

Resilience layer (hybrid upgrade): all keyed providers share a keep-alive
connection pool and route every call through a `ResilientKeyPool` --
comma-separated keys in one env var, per-key RPM pacing, 429 cooldowns,
auth-failure disabling, and a circuit breaker that raises
`ProviderExhaustedError` when no healthy key remains, so the pipeline can hand
the chapter to Agent Mode instead of crashing. `kind: auto` picks the first
provider kind whose key env is actually set (gemini -> openrouter -> nvidia ->
openai -> anthropic), or raises `ProviderExhaustedError` when none is.

To add a backend, subclass Provider and register it in `build_provider`.
"""

from __future__ import annotations

import os
import random
import threading
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

import requests
from requests.adapters import HTTPAdapter

from .images import encode_image
from .util import extract_json  # noqa: F401 - re-exported


class ProviderError(RuntimeError):
    pass


class ProviderExhaustedError(ProviderError):
    """No API keys are configured, or every key/provider has failed or tripped
    its circuit breaker. The pipeline catches this and shifts the remaining
    tiles to Agent Mode (TASK_k.md shards) instead of crashing."""


# One shared keep-alive session for every provider call: connection reuse cuts
# the TLS handshake out of each round-trip. (HTTP/1.1 keep-alive pooling --
# true HTTP/2 multiplexing would need an extra dependency; the win here comes
# from not rebuilding connections per tile.)
_SESSION = requests.Session()
_ADAPTER = HTTPAdapter(pool_connections=8, pool_maxsize=16, max_retries=0)
_SESSION.mount("https://", _ADAPTER)
_SESSION.mount("http://", _ADAPTER)


def _retry_after_seconds(exc: Exception) -> Optional[float]:
    resp = getattr(exc, "response", None)
    header = getattr(resp, "headers", {}).get("Retry-After") if resp is not None else None
    try:
        return float(header) if header else None
    except (TypeError, ValueError):
        return None


@dataclass
class KeyState:
    key: str
    provider_kind: str
    rpm_limit: int = 14
    cooldown_until: float = 0.0
    last_used: float = 0.0
    consecutive_failures: int = 0
    disabled: bool = False

    @property
    def min_interval(self) -> float:
        return 60.0 / max(1, self.rpm_limit)


class ResilientKeyPool:
    """Thread-safe key rotator: RPM pacing, 429 cooldowns, auth disabling and
    a per-key circuit breaker. acquire_slot() blocks (at most `max_wait`)
    until a healthy key is ready, or raises ProviderExhaustedError the moment
    no key can ever become usable -- with zero keys that is immediate, which
    is what lets a keyless machine fall through to Agent Mode in < 1 second.
    """

    def __init__(self, provider_configs: List[Dict[str, Any]],
                 circuit_breaker_threshold: int = 3, max_wait: float = 120.0):
        self.slots: List[KeyState] = []
        self.lock = threading.Lock()
        self.circuit_breaker_threshold = max(1, int(circuit_breaker_threshold))
        self.max_wait = max_wait
        self.last_failures: List[str] = []   # recent classifications, for diagnostics
        self._load(provider_configs)

    def _note(self, what: str) -> None:
        with self.lock:
            self.last_failures.append(what)
            del self.last_failures[:-8]

    def state(self) -> str:
        with self.lock:
            bits = [f"{s.provider_kind}:{'OFF' if s.disabled else 'ok'}/strikes={s.consecutive_failures}"
                    for s in self.slots] or ["no slots loaded (env empty?)"]
            recent = ("; recent: " + ", ".join(self.last_failures[-3:])) if self.last_failures else ""
            return f"[{', '.join(bits)}]{recent}"

    def _load(self, provider_configs: List[Dict[str, Any]]) -> None:
        for cfg in provider_configs:
            kind = str(cfg.get("kind", ""))
            env_name = cfg.get("api_key_env") or ""
            raw = os.environ.get(env_name, "").strip() if env_name else ""
            if not raw:
                continue
            rpm = int(cfg.get("rpm_limit", 14) or 14)
            for k in (k.strip() for k in raw.split(",")):
                if k:
                    self.slots.append(KeyState(key=k, provider_kind=kind, rpm_limit=rpm))

    def has_active_keys(self) -> bool:
        with self.lock:
            return any(not s.disabled for s in self.slots)

    def acquire_slot(self) -> KeyState:
        deadline = time.monotonic() + self.max_wait
        while True:
            with self.lock:
                active = [s for s in self.slots if not s.disabled]
                if not active:
                    exhausted = True
                else:
                    exhausted = False
                    now = time.monotonic()
                    candidates = sorted(
                        ((max(s.cooldown_until, s.last_used + s.min_interval), s) for s in active),
                        key=lambda item: item[0],
                    )
                    soonest, best = candidates[0]
                    if soonest <= now:
                        best.last_used = now
                        return best
                    wait = soonest - now
            if exhausted:
                # state() takes the lock itself: call it OUTSIDE the with-block
                raise ProviderExhaustedError(
                    "No healthy API keys available (none configured, or all "
                    f"cooldown/disabled). Pool state: {self.state()}"
                )
            if time.monotonic() + wait > deadline:
                raise ProviderExhaustedError(
                    f"Every API key is cooling down for another {wait:.0f}s (past the "
                    f"{self.max_wait:.0f}s wait cap). Falling back to Agent Mode."
                )
            time.sleep(min(wait, 1.0))

    def report_success(self, slot: KeyState) -> None:
        self._note("success")
        with self.lock:
            slot.consecutive_failures = 0

    def report_rate_limit(self, slot: KeyState, retry_after: Optional[float] = None) -> None:
        self._note(f"429/402-cooldown({retry_after:.0f}s)" if retry_after else "429/402-cooldown")
        with self.lock:
            wait = retry_after if (retry_after and retry_after > 0) else 15.0
            slot.cooldown_until = time.monotonic() + wait + random.uniform(1.0, 3.5)
            slot.consecutive_failures += 1
            if slot.consecutive_failures >= self.circuit_breaker_threshold * 2:
                slot.disabled = True

    def report_fatal(self, slot: KeyState, why: str = "401/403") -> None:
        """Auth/billing failures (401/402/403): the key is dead, disable now."""
        self._note(f"fatal-disable({why})")
        with self.lock:
            slot.disabled = True

    def report_error(self, slot: KeyState, why: str = "5xx/net") -> None:
        self._note(f"error({why})")
        with self.lock:
            slot.consecutive_failures += 1
            slot.cooldown_until = time.monotonic() + (2 ** min(slot.consecutive_failures, 6))
            if slot.consecutive_failures >= self.circuit_breaker_threshold:
                slot.disabled = True


def _retry(fn, *, attempts: int, label: str):
    last: Exception | None = None
    for i in range(attempts):
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001 - re-raised below
            last = exc
            status = getattr(getattr(exc, "response", None), "status_code", None)
            # Client errors other than rate limiting won't improve on retry.
            if status and status < 500 and status != 429:
                raise ProviderError(f"{label}: {exc}") from exc
            time.sleep(min(2**i, 30))
    raise ProviderError(f"{label} failed after {attempts} attempts: {last}")


class Provider(ABC):
    """Base interface. Two calls: one with images, one without."""

    # True for providers that need a key (routed through the ResilientKeyPool);
    # False for local backends like Ollama that use plain retries.
    KEYED = True

    def __init__(self, cfg: Dict[str, Any]):
        self.cfg = cfg
        self.vision_model = cfg.get("vision_model")
        self.text_model = cfg.get("text_model") or self.vision_model
        self.temperature = cfg.get("temperature", 0.0)
        self.max_tokens = cfg.get("max_tokens", 4096)
        # Text calls carry a whole chapter (assembly) or long narration, so they
        # get their own, larger budget. Truncated output means unparseable JSON.
        self.text_max_tokens = cfg.get("text_max_tokens", 16000)
        self.timeout = cfg.get("timeout", 180)
        self.attempts = cfg.get("max_retries", 4)
        self.base_url = cfg.get("base_url")
        self.api_key = os.environ.get(cfg.get("api_key_env") or "", "")
        # Tile payload shaping: tiles are re-encoded as compressed JPEG at a
        # bounded width before base64 (see images.encode_image).
        self.image_max_width = int(cfg.get("image_max_width", 800) or 800)
        self.image_quality = int(cfg.get("image_quality", 85) or 85)
        if self.KEYED:
            self.pool = ResilientKeyPool(
                [dict(cfg)], circuit_breaker_threshold=int(cfg.get("circuit_breaker_threshold", 3)),
                # How long a worker waits out cooldowns (429 Retry-After,
                # OpenRouter in-flight budget) before declaring exhaustion and
                # handing its tiles to Agent Mode. Small-balance keys need
                # headroom above OpenRouter's 120s in-flight Retry-After.
                max_wait=float(cfg.get("key_wait_seconds", 120)),
            )
        else:
            self.pool = None  # type: ignore[assignment]

    # ---------------- shared transport ----------------

    def _keyed_post(self, url: str, auth_fn: Callable[[str], Dict[str, str]],
                    payload: Dict[str, Any], label: str) -> "requests.Response":
        """POST through the key pool: rotate keys, pace RPM, cool down on 429,
        disable on auth failures, circuit-break to ProviderExhaustedError."""
        last: Exception | None = None
        for attempt in range(max(1, self.attempts)):
            slot = self.pool.acquire_slot()   # ProviderExhaustedError propagates
            try:
                resp = _SESSION.post(url, headers=auth_fn(slot.key), json=payload,
                                     timeout=self.timeout)
                resp.raise_for_status()
                self.pool.report_success(slot)
                return resp
            except Exception as exc:  # noqa: BLE001 - classified below
                last = exc
                status = getattr(getattr(exc, "response", None), "status_code", None)
                if status == 429:
                    self.pool.report_rate_limit(slot, _retry_after_seconds(exc))
                elif status in (401, 403):
                    self.pool.report_fatal(slot, f"auth({status})")
                elif status == 402 and _retry_after_seconds(exc) is not None:
                    # OpenRouter's in-flight budget returns 402 WITH a
                    # Retry-After ("wait for in-flight requests to settle")
                    # while the key is perfectly alive: cooldown, not kill.
                    self.pool.report_rate_limit(slot, _retry_after_seconds(exc))
                elif status == 402:
                    self.pool.report_fatal(slot, "credit(402)")   # out of credit, no retry hint
                else:
                    self.pool.report_error(slot, f"http({status})" if status else type(exc).__name__)
                    if status is None or status >= 500:
                        time.sleep(min(2 ** attempt, 8))   # transient network / 5xx
        raise ProviderError(f"{label} failed after {self.attempts} attempts: {last}")

    def _plain_post(self, url: str, headers: Dict[str, str], payload: Dict[str, Any],
                    label: str) -> "requests.Response":
        """POST for keyless (local) providers: keep-alive session, plain retries."""
        def go() -> "requests.Response":
            resp = _SESSION.post(url, headers=headers, json=payload, timeout=self.timeout)
            resp.raise_for_status()
            return resp
        return _retry(go, attempts=self.attempts, label=label)

    def _encode(self, path: Path) -> tuple[str, str]:
        return encode_image(path, max_width=self.image_max_width, jpeg_quality=self.image_quality)

    @abstractmethod
    def vision(self, system: str, prompt: str, images: List[Path]) -> str: ...

    @abstractmethod
    def text(self, system: str, prompt: str) -> str: ...

    def fingerprint(self) -> str:
        return f"{type(self).__name__}:{self.vision_model}:{self.temperature}"


class AnthropicProvider(Provider):
    def __init__(self, cfg: Dict[str, Any]):
        super().__init__(cfg)
        self.base_url = (self.base_url or "https://api.anthropic.com").rstrip("/")
        if not self.pool.has_active_keys():
            raise ProviderExhaustedError(
                f"No API key. Set ${cfg.get('api_key_env', 'ANTHROPIC_API_KEY')}."
            )

    def _call(
        self, model: str, system: str, content: List[Dict[str, Any]], limit: int | None = None
    ) -> str:
        resp = self._keyed_post(
            f"{self.base_url}/v1/messages",
            lambda key: {
                "x-api-key": key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            {
                "model": model,
                "max_tokens": limit or self.max_tokens,
                "temperature": self.temperature,
                "system": system,
                "messages": [{"role": "user", "content": content}],
            },
            label="anthropic",
        )
        blocks = resp.json().get("content", [])
        return "".join(b.get("text", "") for b in blocks if b.get("type") == "text")

    def vision(self, system: str, prompt: str, images: List[Path]) -> str:
        content: List[Dict[str, Any]] = []
        for img in images:
            data, media = self._encode(img)
            content.append(
                {
                    "type": "image",
                    "source": {"type": "base64", "media_type": media, "data": data},
                }
            )
        content.append({"type": "text", "text": prompt})
        return self._call(self.vision_model, system, content)

    def text(self, system: str, prompt: str) -> str:
        return self._call(
            self.text_model, system, [{"type": "text", "text": prompt}], self.text_max_tokens
        )


class OpenAIProvider(Provider):
    """Any OpenAI-compatible /chat/completions endpoint."""

    def __init__(self, cfg: Dict[str, Any]):
        super().__init__(cfg)
        self.base_url = (self.base_url or "https://api.openai.com/v1").rstrip("/")

    def _call(
        self, model: str, messages: List[Dict[str, Any]], limit: int | None = None
    ) -> str:
        def auth(key: str) -> Dict[str, str]:
            headers = {"content-type": "application/json"}
            if key:
                headers["authorization"] = f"Bearer {key}"
            return headers

        resp = self._keyed_post(
            f"{self.base_url}/chat/completions",
            auth,
            {
                "model": model,
                "messages": messages,
                "temperature": self.temperature,
                "max_tokens": limit or self.max_tokens,
            },
            label="openai-compatible",
        )
        return resp.json()["choices"][0]["message"]["content"] or ""

    def vision(self, system: str, prompt: str, images: List[Path]) -> str:
        parts: List[Dict[str, Any]] = []
        for img in images:
            data, media = self._encode(img)
            parts.append(
                {"type": "image_url", "image_url": {"url": f"data:{media};base64,{data}"}}
            )
        parts.append({"type": "text", "text": prompt})
        return self._call(
            self.vision_model,
            [{"role": "system", "content": system}, {"role": "user", "content": parts}],
        )

    def text(self, system: str, prompt: str) -> str:
        return self._call(
            self.text_model,
            [{"role": "system", "content": system}, {"role": "user", "content": prompt}],
            self.text_max_tokens,
        )


class OllamaProvider(Provider):
    """Local models through Ollama's native chat API."""

    KEYED = False   # no key pool: local endpoint, plain retries

    def __init__(self, cfg: Dict[str, Any]):
        super().__init__(cfg)
        self.base_url = (self.base_url or "http://localhost:11434").rstrip("/")

    def _call(
        self, model: str, messages: List[Dict[str, Any]], limit: int | None = None
    ) -> str:
        resp = self._plain_post(
            f"{self.base_url}/api/chat",
            {"content-type": "application/json"},
            {
                "model": model,
                "messages": messages,
                "stream": False,
                "options": {
                    "temperature": self.temperature,
                    "num_predict": limit or self.max_tokens,
                },
            },
            label="ollama",
        )
        return resp.json().get("message", {}).get("content", "")

    def vision(self, system: str, prompt: str, images: List[Path]) -> str:
        b64 = [self._encode(p)[0] for p in images]
        return self._call(
            self.vision_model,
            [
                {"role": "system", "content": system},
                {"role": "user", "content": prompt, "images": b64},
            ],
        )

    def text(self, system: str, prompt: str) -> str:
        return self._call(
            self.text_model,
            [{"role": "system", "content": system}, {"role": "user", "content": prompt}],
            self.text_max_tokens,
        )


class GeminiProvider(Provider):
    """Google Gemini via the native generateContent API.

    Useful if you already hold a Gemini key -- for instance when driving this
    tool from Antigravity. Google also exposes an OpenAI-compatible endpoint, so
    `kind: openai` with base_url
    https://generativelanguage.googleapis.com/v1beta/openai
    works too; this native path avoids that shim.
    """

    def __init__(self, cfg: Dict[str, Any]):
        super().__init__(cfg)
        self.base_url = (
            self.base_url or "https://generativelanguage.googleapis.com/v1beta"
        ).rstrip("/")
        if not self.pool.has_active_keys():
            raise ProviderExhaustedError(
                f"No API key. Set ${cfg.get('api_key_env', 'GEMINI_API_KEY')}."
            )

    def _call(
        self, model: str, system: str, parts: List[Dict[str, Any]], limit: int | None = None
    ) -> str:
        resp = self._keyed_post(
            f"{self.base_url}/models/{model}:generateContent",
            lambda key: {
                "content-type": "application/json",
                "x-goog-api-key": key,
            },
            {
                "system_instruction": {"parts": [{"text": system}]},
                "contents": [{"role": "user", "parts": parts}],
                "generationConfig": {
                    "temperature": self.temperature,
                    "maxOutputTokens": limit or self.max_tokens,
                },
            },
            label="gemini",
        )
        candidates = resp.json().get("candidates", [])
        if not candidates:
            return ""
        chunks = candidates[0].get("content", {}).get("parts", [])
        return "".join(c.get("text", "") for c in chunks)

    def vision(self, system: str, prompt: str, images: List[Path]) -> str:
        parts: List[Dict[str, Any]] = []
        for img in images:
            data, media = self._encode(img)
            parts.append({"inline_data": {"mime_type": media, "data": data}})
        parts.append({"text": prompt})
        return self._call(self.vision_model, system, parts)

    def text(self, system: str, prompt: str) -> str:
        return self._call(self.text_model, system, [{"text": prompt}], self.text_max_tokens)


PROVIDERS = {
    "anthropic": AnthropicProvider,
    "claude": AnthropicProvider,
    "openai": OpenAIProvider,
    "openrouter": OpenAIProvider,
    "nvidia": OpenAIProvider,
    "openai-compatible": OpenAIProvider,
    "ollama": OllamaProvider,
    "gemini": GeminiProvider,
    "google": GeminiProvider,
}


# What `kind: auto` falls through when the configured kind has no usable key:
# the first entry whose env var is actually set wins. All OpenAI-compatible
# endpoints (openrouter, nvidia) reuse OpenAIProvider with their own base_url.
# Every entry pins its own model, so a cfg-level vision_model never leaks into
# a provider it doesn't belong to.
AUTO_KINDS: List[Dict[str, Any]] = [
    {"kind": "gemini", "api_key_env": "GEMINI_API_KEY", "vision_model": "gemini-3.8-flash",
     "rpm_limit": 14},
    {"kind": "openrouter", "api_key_env": "OPENROUTER_API_KEY",
     "base_url": "https://openrouter.ai/api/v1", "vision_model": "qwen/qwen-2.5-vl-72b-instruct",
     "rpm_limit": 40},
    {"kind": "nvidia", "api_key_env": "NVIDIA_API_KEY",
     "base_url": "https://integrate.api.nvidia.com/v1",
     "vision_model": "meta/llama-3.2-90b-vision-instruct", "rpm_limit": 35},
    {"kind": "openai", "api_key_env": "OPENAI_API_KEY", "vision_model": "gpt-4o-mini"},
    {"kind": "anthropic", "api_key_env": "ANTHROPIC_API_KEY",
     "vision_model": "claude-haiku-4-5-20251001"},
]


def _auto_candidates(cfg: Dict[str, Any]) -> List[Dict[str, Any]]:
    """The ordered provider chain for `kind: auto`: the config's own
    `fallback_providers` entries first (they carry their own kind/env/model),
    then the built-in AUTO_KINDS priority list. Entries whose env var has no
    keys are skipped without touching the network."""
    chain: List[Dict[str, Any]] = []
    seen: set[str] = set()

    def add(entry: Dict[str, Any]) -> None:
        kind = str(entry.get("kind") or "")
        env = str(entry.get("api_key_env") or "")
        if not kind or kind in ("agent", "auto") or (kind, env) in seen:
            return
        if not os.environ.get(env, "").strip():
            return   # no keys in this env: skip without touching the network
        seen.add((kind, env))
        merged = {k: v for k, v in cfg.items() if k not in ("kind", "fallback_providers")}
        merged.update({k: v for k, v in entry.items() if v is not None})
        chain.append(merged)

    for entry in cfg.get("fallback_providers") or []:
        if isinstance(entry, dict):
            add(entry)
    for entry in AUTO_KINDS:
        add(entry)
    return chain


# OpenAI-compatible runners that need their own endpoint to be usable.
_KIND_BASE_URLS = {
    "openrouter": "https://openrouter.ai/api/v1",
    "nvidia": "https://integrate.api.nvidia.com/v1",
}


def build_provider(cfg: Dict[str, Any]) -> Provider:
    kind = (cfg.get("kind") or "anthropic").lower()
    if kind == "auto":
        for entry in _auto_candidates(cfg):
            try:
                entry = {**entry}
                if not entry.get("base_url") and _KIND_BASE_URLS.get(entry.get("kind", "")):
                    entry["base_url"] = _KIND_BASE_URLS[entry["kind"]]
                return PROVIDERS[entry["kind"]](entry)
            except ProviderExhaustedError:
                continue   # raced past the env check (e.g. whitespace-only key)
        raise ProviderExhaustedError(
            "No API keys detected (checked GEMINI_API_KEY, OPENROUTER_API_KEY, "
            "NVIDIA_API_KEY, OPENAI_API_KEY, ANTHROPIC_API_KEY). Configure one, or "
            "run agent mode: the pipeline will shift the tiles to TASK_k.md shards."
        )
    if kind not in PROVIDERS:
        raise ProviderError(
            f"Unknown provider '{kind}'. Options: {', '.join(sorted(PROVIDERS))}, auto"
        )
    if _KIND_BASE_URLS.get(kind) and not cfg.get("base_url"):
        cfg = {**cfg, "base_url": _KIND_BASE_URLS[kind]}
    return PROVIDERS[kind](cfg)
