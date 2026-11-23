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
        temperature: float = 0.0,
        max_retries: int = 2,
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
        self._temperature = temperature
        self._max_retries = max_retries
        self._transport = transport

    @property
    def name(self) -> str:
        return f"openai_compat:{self._model}"

    def _request_body(self, *, system: str, user: str) -> dict[str, object]:
        body: dict[str, object] = {
            "model": self._model,
            "temperature": self._temperature,
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
        return body

    def _post_with_retries(
        self, url: str, headers: dict[str, str], json_body: dict[str, object]
    ) -> httpx.Response:
        """POST with bounded retries on transient failures only.

        Retries timeouts, connection errors, 429s (honoring Retry-After),
        and 5xxs. Other 4xx responses are request errors, not worth
        repeating, and raise immediately.
        """
        last_error = "unknown error"
        with httpx.Client(timeout=self._timeout_s, transport=self._transport) as http:
            for attempt in range(self._max_retries + 1):
                try:
                    resp = http.post(url, headers=headers, json=json_body)
                except (httpx.TimeoutException, httpx.ConnectError) as exc:
                    last_error = f"{type(exc).__name__}: {exc}"
                else:
                    status = resp.status_code
                    if status < 400:
                        return resp
                    if status == 429 or 500 <= status < 600:
                        last_error = f"HTTP {status}"
                        if attempt < self._max_retries:
                            retry_after = resp.headers.get("retry-after")
                            delay = self._retry_delay(attempt, retry_after)
                            log.info(
                                "llm.retry",
                                provider=self.name,
                                status=status,
                                attempt=attempt + 1,
                                delay_s=round(delay, 1),
                            )
                            time.sleep(delay)
                        continue
                    raise LLMError(f"OpenAI-compatible call failed: HTTP {status}")
                if attempt < self._max_retries:
                    delay = self._retry_delay(attempt, None)
                    log.info(
                        "llm.retry",
                        provider=self.name,
                        error=last_error,
                        attempt=attempt + 1,
                        delay_s=round(delay, 1),
                    )
                    time.sleep(delay)
        raise LLMError(f"OpenAI-compatible call failed after retries: {last_error}")

    @staticmethod
    def _retry_delay(attempt: int, retry_after: str | None) -> float:
        if retry_after:
            try:
                return min(float(retry_after), 60.0)
            except ValueError:
                pass
        return min(2.0**attempt, 8.0)

    def complete(self, *, system: str, user: str) -> LLMResponse:
        started = time.monotonic()
        resp = self._post_with_retries(
            f"{self._base_url}/chat/completions",
            headers={"Authorization": f"Bearer {self._api_key}"},
            json_body=self._request_body(system=system, user=user),
        )
        try:
            payload = resp.json()
        except ValueError as exc:
            raise LLMError(f"Unexpected provider response shape: {exc}") from exc
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
