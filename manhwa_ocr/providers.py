"""Vision/text model backends.

Everything speaks plain HTTP via `requests`, so there are no vendor SDK
dependencies to keep in sync. Four kinds cover essentially every runner:

  anthropic  -> Claude API
  gemini     -> Google Gemini native generateContent API
  openai     -> any OpenAI-compatible /chat/completions endpoint. That includes
                OpenAI, OpenRouter, Groq, Together, LM Studio, vLLM, llama.cpp
                server, and most local proxies.
  ollama     -> native Ollama /api/chat (llava, llama3.2-vision, qwen2-vl, ...)

To add a backend, subclass Provider and register it in `build_provider`.
"""

from __future__ import annotations

import os
import time
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any, Dict, List

import requests

from .images import encode_image
from .util import extract_json  # noqa: F401 - re-exported


class ProviderError(RuntimeError):
    pass


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
        if not self.api_key:
            raise ProviderError(
                f"No API key. Set ${cfg.get('api_key_env', 'ANTHROPIC_API_KEY')}."
            )

    def _call(
        self, model: str, system: str, content: List[Dict[str, Any]], limit: int | None = None
    ) -> str:
        def go() -> str:
            resp = requests.post(
                f"{self.base_url}/v1/messages",
                headers={
                    "x-api-key": self.api_key,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                },
                json={
                    "model": model,
                    "max_tokens": limit or self.max_tokens,
                    "temperature": self.temperature,
                    "system": system,
                    "messages": [{"role": "user", "content": content}],
                },
                timeout=self.timeout,
            )
            resp.raise_for_status()
            blocks = resp.json().get("content", [])
            return "".join(b.get("text", "") for b in blocks if b.get("type") == "text")

        return _retry(go, attempts=self.attempts, label="anthropic")

    def vision(self, system: str, prompt: str, images: List[Path]) -> str:
        content: List[Dict[str, Any]] = []
        for img in images:
            data, media = encode_image(img)
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
        headers = {"content-type": "application/json"}
        if self.api_key:
            headers["authorization"] = f"Bearer {self.api_key}"

        def go() -> str:
            resp = requests.post(
                f"{self.base_url}/chat/completions",
                headers=headers,
                json={
                    "model": model,
                    "messages": messages,
                    "temperature": self.temperature,
                    "max_tokens": limit or self.max_tokens,
                },
                timeout=self.timeout,
            )
            resp.raise_for_status()
            return resp.json()["choices"][0]["message"]["content"] or ""

        return _retry(go, attempts=self.attempts, label="openai-compatible")

    def vision(self, system: str, prompt: str, images: List[Path]) -> str:
        parts: List[Dict[str, Any]] = []
        for img in images:
            data, media = encode_image(img)
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

    def __init__(self, cfg: Dict[str, Any]):
        super().__init__(cfg)
        self.base_url = (self.base_url or "http://localhost:11434").rstrip("/")

    def _call(
        self, model: str, messages: List[Dict[str, Any]], limit: int | None = None
    ) -> str:
        def go() -> str:
            resp = requests.post(
                f"{self.base_url}/api/chat",
                json={
                    "model": model,
                    "messages": messages,
                    "stream": False,
                    "options": {
                        "temperature": self.temperature,
                        "num_predict": limit or self.max_tokens,
                    },
                },
                timeout=self.timeout,
            )
            resp.raise_for_status()
            return resp.json().get("message", {}).get("content", "")

        return _retry(go, attempts=self.attempts, label="ollama")

    def vision(self, system: str, prompt: str, images: List[Path]) -> str:
        b64 = [encode_image(p)[0] for p in images]
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
        if not self.api_key:
            raise ProviderError(
                f"No API key. Set ${cfg.get('api_key_env', 'GEMINI_API_KEY')}."
            )

    def _call(
        self, model: str, system: str, parts: List[Dict[str, Any]], limit: int | None = None
    ) -> str:
        def go() -> str:
            resp = requests.post(
                f"{self.base_url}/models/{model}:generateContent",
                headers={
                    "content-type": "application/json",
                    "x-goog-api-key": self.api_key,
                },
                json={
                    "system_instruction": {"parts": [{"text": system}]},
                    "contents": [{"role": "user", "parts": parts}],
                    "generationConfig": {
                        "temperature": self.temperature,
                        "maxOutputTokens": limit or self.max_tokens,
                    },
                },
                timeout=self.timeout,
            )
            resp.raise_for_status()
            candidates = resp.json().get("candidates", [])
            if not candidates:
                return ""
            chunks = candidates[0].get("content", {}).get("parts", [])
            return "".join(c.get("text", "") for c in chunks)

        return _retry(go, attempts=self.attempts, label="gemini")

    def vision(self, system: str, prompt: str, images: List[Path]) -> str:
        parts: List[Dict[str, Any]] = []
        for img in images:
            data, media = encode_image(img)
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
    "openai-compatible": OpenAIProvider,
    "ollama": OllamaProvider,
    "gemini": GeminiProvider,
    "google": GeminiProvider,
}


def build_provider(cfg: Dict[str, Any]) -> Provider:
    kind = (cfg.get("kind") or "anthropic").lower()
    if kind not in PROVIDERS:
        raise ProviderError(
            f"Unknown provider '{kind}'. Options: {', '.join(sorted(PROVIDERS))}"
        )
    return PROVIDERS[kind](cfg)
