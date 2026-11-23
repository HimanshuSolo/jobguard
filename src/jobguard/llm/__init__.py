"""LLM layer: interface, adapters, schemas."""

from jobguard.llm.client import LLMClient, LLMError, LLMResponse
from jobguard.llm.fake import FakeClient
from jobguard.llm.openai_compat import OpenAICompatClient
from jobguard.llm.replay import RecordReplayClient, prompt_key
from jobguard.llm.schemas import (
    Classification,
    ClassifyExtract,
    MatchAndSuggest,
    MatchResult,
    PostingExtraction,
    SchemaValidationError,
    TailoringCard,
    TailoringSuggestion,
    parse_strict,
    schema_hint,
)

__all__ = [
    "Classification",
    "ClassifyExtract",
    "FakeClient",
    "LLMClient",
    "LLMError",
    "LLMResponse",
    "MatchResult",
    "OpenAICompatClient",
    "PostingExtraction",
    "RecordReplayClient",
    "SchemaValidationError",
    "MatchAndSuggest",
    "TailoringCard",
    "TailoringSuggestion",
    "parse_strict",
    "prompt_key",
    "schema_hint",
]
