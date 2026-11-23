"""Build the configured LLM client stack (Phase 4, throttled in Phase 8).

Order: provider adapter (behind a shared token-bucket limiter) -> optional
circuit breaker -> optional fallback to a secondary provider -> optional
record/replay wrapper. Pure composition; every layer honors the same
`LLMClient` protocol.
"""

from __future__ import annotations

from jobguard.agent.fallback import CircuitBreaker, FallbackClient
from jobguard.agent.ratelimit import RateLimiter
from jobguard.config import Settings
from jobguard.llm.client import LLMClient
from jobguard.llm.fake import FakeClient
from jobguard.llm.openai_compat import OpenAICompatClient
from jobguard.llm.replay import RecordReplayClient


def build_client(settings: Settings) -> LLMClient:
    """Compose the client stack from settings (raises in DEMO_MODE for live)."""
    provider = settings.llm_provider.lower()
    limiter: RateLimiter | None = None
    if provider != "fake":
        limiter = RateLimiter(
            requests_per_minute=settings.llm_max_requests_per_min,
            tokens_per_minute=settings.llm_max_tokens_per_min,
        )
    if provider == "fake":
        client: LLMClient = FakeClient()
    elif provider == "openai_compat":
        settings.require_live_credentials()
        if not settings.llm_base_url or not settings.llm_model:
            raise ValueError("openai_compat needs LLM_BASE_URL and LLM_MODEL")
        client = OpenAICompatClient(
            base_url=settings.llm_base_url,
            api_key=settings.llm_api_key,
            model=settings.llm_model,
            limiter=limiter,
        )
    else:
        raise ValueError(f"Unknown LLM_PROVIDER {settings.llm_provider!r}")

    if provider != "fake" and settings.llm_secondary_model:
        if not settings.llm_secondary_base_url:
            raise ValueError("secondary provider needs LLM_SECONDARY_BASE_URL")
        secondary = OpenAICompatClient(
            base_url=settings.llm_secondary_base_url,
            api_key=settings.llm_secondary_api_key,
            model=settings.llm_secondary_model,
            limiter=limiter,
        )
        client = FallbackClient(
            CircuitBreaker(client, threshold=settings.llm_circuit_threshold),
            secondary,
        )

    if settings.llm_replay_path:
        client = RecordReplayClient(client, settings.llm_replay_path, mode=settings.llm_replay_mode)
    return client
