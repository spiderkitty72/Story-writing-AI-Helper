"""Pluggable LLM providers.

Add a new provider by subclassing :class:`Provider` and registering it in
``PROVIDER_TYPES``. Providers are configured in ``config/settings.yaml``.
"""
from __future__ import annotations

from typing import Any

import httpx


class ProviderError(RuntimeError):
    pass


class Provider:
    type_name = "base"

    def __init__(self, name: str, options: dict[str, Any]):
        self.name = name
        self.options = options
        self.model = options.get("model", "")
        self.timeout = float(options.get("timeout", 300))
        # Set by complete(): True when the model stopped because it hit its
        # output limit, meaning the answer is cut off.
        self.truncated = False

    async def complete(self, system: str, prompt: str, *, model: str | None = None,
                       temperature: float | None = None) -> str:
        raise NotImplementedError

    async def check(self) -> str:
        """Return a short status string, raising ProviderError if unusable."""
        return "configured"

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(timeout=self.timeout)


class EchoProvider(Provider):
    """Returns the text it was asked to edit, unchanged. For testing the UI."""

    type_name = "echo"

    async def complete(self, system, prompt, *, model=None, temperature=None):
        from app.prompts import REPORT_RULES, extract_target

        target = extract_target(prompt)
        if target is None:
            return '{"entities": [], "relations": []}'
        if REPORT_RULES in prompt:
            return "No issues found."
        return target

    async def check(self):
        return "ok (returns text unchanged)"


class OllamaProvider(Provider):
    type_name = "ollama"

    @property
    def base_url(self) -> str:
        return self.options.get("base_url", "http://localhost:11434").rstrip("/")

    async def complete(self, system, prompt, *, model=None, temperature=None):
        # Ollama silently drops the START of a prompt that does not fit in
        # num_ctx, which loses the instructions. Make sure it always fits,
        # with room for an answer up to twice as long as the prompt's passage.
        prompt_tokens = (len(system) + len(prompt)) // 3 + 64
        num_predict = int(self.options.get("num_predict", 4096))
        num_ctx = max(int(self.options.get("num_ctx", 8192)), prompt_tokens + num_predict)
        max_ctx = int(self.options.get("max_ctx", 32768))
        num_ctx = min(num_ctx, max_ctx)
        body: dict[str, Any] = {
            "model": model or self.model,
            "stream": False,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": prompt},
            ],
            "options": {"num_ctx": num_ctx, "num_predict": num_predict},
        }
        if temperature is not None:
            body["options"]["temperature"] = temperature
        async with self._client() as c:
            try:
                r = await c.post(f"{self.base_url}/api/chat", json=body)
            except httpx.HTTPError as e:
                raise ProviderError(f"Cannot reach Ollama at {self.base_url}: {e}") from e
        if r.status_code != 200:
            raise ProviderError(f"Ollama error {r.status_code}: {r.text[:300]}")
        data = r.json()
        self.truncated = data.get("done_reason") == "length"
        return data.get("message", {}).get("content", "")

    async def check(self):
        async with self._client() as c:
            try:
                r = await c.get(f"{self.base_url}/api/tags")
                r.raise_for_status()
            except httpx.HTTPError as e:
                raise ProviderError(f"Cannot reach Ollama at {self.base_url}") from e
        models = [m.get("name") for m in r.json().get("models", [])]
        if self.model and self.model not in models and f"{self.model}:latest" not in models:
            return f"reachable, but model '{self.model}' is not pulled (have: {', '.join(models) or 'none'})"
        return "ok"

    async def list_models(self) -> list[str]:
        async with self._client() as c:
            r = await c.get(f"{self.base_url}/api/tags")
            r.raise_for_status()
        return [m.get("name") for m in r.json().get("models", [])]


class AnthropicProvider(Provider):
    type_name = "anthropic"
    url = "https://api.anthropic.com/v1/messages"

    async def complete(self, system, prompt, *, model=None, temperature=None):
        key = self.options.get("api_key")
        if not key:
            raise ProviderError("ANTHROPIC_API_KEY is not set")
        body: dict[str, Any] = {
            "model": model or self.model,
            "max_tokens": int(self.options.get("max_tokens", 8000)),
            "system": system,
            "messages": [{"role": "user", "content": prompt}],
        }
        if temperature is not None:
            body["temperature"] = temperature
        headers = {"x-api-key": key, "anthropic-version": "2023-06-01", "content-type": "application/json"}
        async with self._client() as c:
            try:
                r = await c.post(self.options.get("base_url", self.url), json=body, headers=headers)
            except httpx.HTTPError as e:
                raise ProviderError(f"Cannot reach Anthropic API: {e}") from e
        if r.status_code != 200:
            raise ProviderError(f"Claude error {r.status_code}: {r.text[:300]}")
        data = r.json()
        self.truncated = data.get("stop_reason") == "max_tokens"
        return "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text")

    async def check(self):
        if not self.options.get("api_key"):
            raise ProviderError("ANTHROPIC_API_KEY is not set")
        return "api key set"


class GeminiProvider(Provider):
    type_name = "gemini"
    base = "https://generativelanguage.googleapis.com/v1beta/models"

    async def complete(self, system, prompt, *, model=None, temperature=None):
        key = self.options.get("api_key")
        if not key:
            raise ProviderError("GEMINI_API_KEY is not set")
        gen: dict[str, Any] = {"maxOutputTokens": int(self.options.get("max_tokens", 8000))}
        if temperature is not None:
            gen["temperature"] = temperature
        body = {
            "systemInstruction": {"parts": [{"text": system}]},
            "contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "generationConfig": gen,
        }
        url = f"{self.options.get('base_url', self.base)}/{model or self.model}:generateContent"
        async with self._client() as c:
            try:
                r = await c.post(url, json=body, headers={"x-goog-api-key": key})
            except httpx.HTTPError as e:
                raise ProviderError(f"Cannot reach Gemini API: {e}") from e
        if r.status_code != 200:
            raise ProviderError(f"Gemini error {r.status_code}: {r.text[:300]}")
        cands = r.json().get("candidates") or []
        if not cands:
            raise ProviderError("Gemini returned no answer (possibly blocked by safety filters)")
        self.truncated = cands[0].get("finishReason") == "MAX_TOKENS"
        parts = cands[0].get("content", {}).get("parts", [])
        return "".join(p.get("text", "") for p in parts)

    async def check(self):
        if not self.options.get("api_key"):
            raise ProviderError("GEMINI_API_KEY is not set")
        return "api key set"


PROVIDER_TYPES: dict[str, type[Provider]] = {
    cls.type_name: cls for cls in (EchoProvider, OllamaProvider, AnthropicProvider, GeminiProvider)
}


def make_provider(name: str, options: dict[str, Any]) -> Provider:
    t = options.get("type")
    if t not in PROVIDER_TYPES:
        raise ProviderError(f"Unknown provider type '{t}' for '{name}'. Known: {', '.join(PROVIDER_TYPES)}")
    return PROVIDER_TYPES[t](name, options)
