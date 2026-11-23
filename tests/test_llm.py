"""Tests for LLM clients. No live network: httpx MockTransport only."""

from __future__ import annotations

import json

import httpx
import pytest

from jobguard.llm.client import LLMError
from jobguard.llm.fake import FakeClient
from jobguard.llm.openai_compat import OpenAICompatClient
from jobguard.llm.replay import RecordReplayClient


def test_fake_first_match_wins() -> None:
    client = FakeClient(
        rules=[("Acme", '{"kind": "posting"}')],
        default='{"kind": "other"}',
    )
    assert "posting" in client.complete(system="s", user="SDE at Acme").text
    assert "other" in client.complete(system="s", user="unrelated").text


def test_fake_records_calls() -> None:
    client = FakeClient()
    client.complete(system="s", user="u")
    assert client.calls == [{"system": "s", "user": "u"}]


def _ok_transport(payload: dict[str, object]) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/chat/completions")
        return httpx.Response(200, json=payload)

    return httpx.MockTransport(handler)


def test_openai_compat_parses_response() -> None:
    transport = _ok_transport(
        {
            "choices": [{"message": {"content": '{"kind": "posting"}'}}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5},
        }
    )
    client = OpenAICompatClient(
        base_url="https://api.example.com/v1",
        api_key="test-key",
        model="test-model",
        transport=transport,
    )
    resp = client.complete(system="s", user="u")
    assert resp.text == '{"kind": "posting"}'
    assert resp.input_tokens == 10
    assert resp.output_tokens == 5


def test_openai_compat_sends_temperature_and_json_mode() -> None:
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        import json as _json

        seen.update(_json.loads(request.content.decode()))
        return httpx.Response(200, json={"choices": [{"message": {"content": "{}"}}]})

    client = OpenAICompatClient(
        base_url="https://api.example.com/v1",
        api_key="k",
        model="m",
        transport=httpx.MockTransport(handler),
    )
    client.complete(system="s", user="u")
    assert seen["temperature"] == 0.0  # deterministic extraction, not sampling
    assert seen["response_format"] == {"type": "json_object"}


def test_openai_compat_http_error_raises() -> None:
    calls = {"n": 0}

    def handler(_: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(500, json={"error": "boom"})

    client = OpenAICompatClient(
        base_url="https://api.example.com/v1",
        api_key="k",
        model="m",
        max_retries=0,  # no sleeping in tests
        transport=httpx.MockTransport(handler),
    )
    with pytest.raises(LLMError):
        client.complete(system="s", user="u")
    assert calls["n"] == 1


def test_openai_compat_retries_then_succeeds() -> None:
    calls = {"n": 0}

    def handler(_: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] < 3:
            return httpx.Response(429, content=b"slow down")
        return httpx.Response(200, json={"choices": [{"message": {"content": "{}"}}]})

    client = OpenAICompatClient(
        base_url="https://api.example.com/v1",
        api_key="k",
        model="m",
        max_retries=5,
        transport=httpx.MockTransport(handler),
    )
    import jobguard.llm.openai_compat as compat

    slept: list[float] = []
    real_sleep = compat.time.sleep
    compat.time.sleep = slept.append  # type: ignore[method-assign]
    try:
        resp = client.complete(system="s", user="u")
    finally:
        compat.time.sleep = real_sleep  # type: ignore[method-assign]
    assert resp.text == "{}"
    assert calls["n"] == 3
    assert slept == [1.0, 2.0]  # exponential backoff, no real waiting


def test_openai_compat_does_not_retry_400() -> None:
    calls = {"n": 0}

    def handler(_: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(400, content=b"bad request")

    client = OpenAICompatClient(
        base_url="https://api.example.com/v1",
        api_key="k",
        model="m",
        max_retries=3,
        transport=httpx.MockTransport(handler),
    )
    with pytest.raises(LLMError, match="HTTP 400"):
        client.complete(system="s", user="u")
    assert calls["n"] == 1  # request errors are not worth repeating


def test_openai_compat_requires_base_url_and_model() -> None:
    with pytest.raises(ValueError):
        OpenAICompatClient(base_url="", api_key="k", model="m")
    with pytest.raises(ValueError):
        OpenAICompatClient(base_url="https://x.example", api_key="k", model="")


def test_replay_roundtrip(tmp_path: object) -> None:
    from pathlib import Path

    path = Path(str(tmp_path)) / "caps.jsonl"
    recorder = RecordReplayClient(FakeClient(default="hello"), path, mode="record")
    first = recorder.complete(system="s", user="u")
    assert path.exists()

    replayer = RecordReplayClient(FakeClient(default="WRONG"), path, mode="replay")
    second = replayer.complete(system="s", user="u")
    assert (first.text, first.model) == (second.text, second.model) == ("hello", "fake-test-model")


def test_replay_miss_raises() -> None:
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        from pathlib import Path

        replayer = RecordReplayClient(FakeClient(), Path(tmp) / "empty.jsonl", mode="replay")
        with pytest.raises(LLMError):
            replayer.complete(system="s", user="never-recorded")


def test_replay_file_is_jsonl(tmp_path: object) -> None:
    from pathlib import Path

    path = Path(str(tmp_path)) / "caps.jsonl"
    RecordReplayClient(FakeClient(default="hi"), path, mode="record").complete(system="s", user="u")
    entry = json.loads(path.read_text(encoding="utf-8").splitlines()[0])
    assert set(entry) == {"key", "response"}
