"""OpenAI-compatible chat-completions adapter (Groq / OpenRouter / Ollama).

Talks to any ``POST {base_url}/chat/completions`` endpoint with the OpenAI
message schema. Timeouts are bounded; the API key is sent as a bearer token
and never logged.
"""

from __future__ import annotations

import time

import httpx
import structlog

from jobguard.llm.client import LLMError, LLMResponse

log = structlog.get_logger(__name__)


class OpenAICompatClient:
    """LLMClient over an OpenAI-compatible HTTP API."""

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        model: str,
        timeout_s: float = 30.0,
        json_mode: bool = True,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        if not base_url:
            raise ValueError("base_url is required for OpenAICompatClient")
        if not model:
            raise ValueError("model is required for OpenAICompatClient")
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._model = model
        self._timeout_s = timeout_s
        self._json_mode = json_mode
        self._transport = transport

    @property
    def name(self) -> str:
        return f"openai_compat:{self._model}"

    def complete(self, *, system: str, user: str) -> LLMResponse:
        started = time.monotonic()
        try:
            with httpx.Client(timeout=self._timeout_s, transport=self._transport) as http:
                body: dict[str, object] = {
                    "model": self._model,
                    "messages": [
                        {"role": "system", "content": system},
                        {"role": "user", "content": user},
                    ],
                }
                if self._json_mode:
                    # Ask OpenAI-compatible endpoints (Groq/OpenRouter/Ollama)
                    # for a JSON object. Strict endpoints that reject the
                    # field can use json_mode=False.
                    body["response_format"] = {"type": "json_object"}
                resp = http.post(
                    f"{self._base_url}/chat/completions",
                    headers={"Authorization": f"Bearer {self._api_key}"},
                    json=body,
                )
                resp.raise_for_status()
                payload = resp.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise LLMError(f"OpenAI-compatible call failed: {exc}") from exc
        try:
            text = payload["choices"][0]["message"]["content"]
            usage = payload.get("usage", {})
        except (KeyError, IndexError, TypeError) as exc:
            raise LLMError(f"Unexpected provider response shape: {exc}") from exc
        latency_ms = int((time.monotonic() - started) * 1000)
        log.info(
            "llm.complete",
            provider=self.name,
            input_tokens=usage.get("prompt_tokens", 0),
            output_tokens=usage.get("completion_tokens", 0),
            latency_ms=latency_ms,
        )
        return LLMResponse(
            model=self._model,
            text=text if isinstance(text, str) else str(text),
            input_tokens=int(usage.get("prompt_tokens", 0)),
            output_tokens=int(usage.get("completion_tokens", 0)),
            latency_ms=latency_ms,
        )
