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


def build_client(settings: Settings, limiter: RateLimiter | None = None) -> LLMClient:
    """Compose the client stack from settings (raises in DEMO_MODE for live)."""
    provider = settings.llm_provider.lower()
    if provider != "fake" and limiter is None:
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


def build_pipeline_clients(
    settings: Settings,
) -> tuple[LLMClient, LLMClient, RateLimiter | None]:
    """Build (smart, fast, limiter) sharing one token bucket.

    The smart stack handles match+suggest; the fast client handles
    classify/extract. Fake provider yields limiter-free clients.
    """
    provider = settings.llm_provider.lower()
    limiter: RateLimiter | None = None
    if provider != "fake":
        limiter = RateLimiter(
            requests_per_minute=settings.llm_max_requests_per_min,
            tokens_per_minute=settings.llm_max_tokens_per_min,
        )
    return build_client(settings, limiter), build_fast_client(settings, limiter), limiter


def build_fast_client(settings: Settings, limiter: RateLimiter | None = None) -> LLMClient:
    """Cheap client for classify/extract steps.

    Uses LLM_FAST_MODEL when configured, else the main model. No circuit
    breaker or fallback: these steps are cheap and retry once, then the
    item skips safely.
    """
    provider = settings.llm_provider.lower()
    if provider == "fake":
        return FakeClient()
    if provider != "openai_compat":
        raise ValueError(f"Unknown LLM_PROVIDER {settings.llm_provider!r}")
    settings.require_live_credentials()
    if not settings.llm_base_url:
        raise ValueError("openai_compat needs LLM_BASE_URL")
    return OpenAICompatClient(
        base_url=settings.llm_base_url,
        api_key=settings.llm_api_key,
        model=settings.llm_fast_model or settings.llm_model,
        limiter=limiter,
    )
