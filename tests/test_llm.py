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


def test_openai_compat_http_error_raises() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"error": "boom"})

    client = OpenAICompatClient(
        base_url="https://api.example.com/v1",
        api_key="k",
        model="m",
        transport=httpx.MockTransport(handler),
    )
    with pytest.raises(LLMError):
        client.complete(system="s", user="u")


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
