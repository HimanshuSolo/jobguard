"""Tests for mailbox sources. Fakes only — never touches a real mailbox."""

from __future__ import annotations

import base64
import email.message
import re
from typing import Any

from jobguard.sources.base import SourceItem
from jobguard.sources.mail import (
    GmailAlertAdapter,
    GmailApiMailSource,
    ImapMailSource,
    MailMessage,
    MailSource,
)


def _gmail_raw(message_id: str) -> dict[str, Any]:
    def part(mime: str, text: str) -> dict[str, Any]:
        return {
            "mimeType": mime,
            "body": {"data": base64.urlsafe_b64encode(text.encode()).decode()},
        }

    return {
        "id": message_id,
        "payload": {
            "headers": [
                {"name": "From", "value": "jobs-noreply@linkedin.com"},
                {"name": "Subject", "value": "SDE Intern jobs"},
            ],
            "parts": [
                part(
                    "text/plain",
                    "Title: SDE Intern\nCompany: Acme\nApply: https://x.example/1",
                )
            ],
        },
    }


class FakeGmailService:
    """Minimal users().messages().list()/get() chain."""

    def __init__(self, ids: list[str]) -> None:
        self._ids = ids
        self._mode: str = "list"

    def users(self) -> FakeGmailService:
        return self

    def messages(self) -> FakeGmailService:
        return self

    def list(self, **_: Any) -> FakeGmailService:
        self._mode = "list"
        return self

    def get(self, **kwargs: Any) -> FakeGmailService:
        self._mode = f"get:{kwargs['id']}"
        return self

    def execute(self) -> dict[str, Any]:
        if self._mode == "list":
            return {"messages": [{"id": i} for i in self._ids]}
        return _gmail_raw(self._mode.removeprefix("get:"))


def test_gmail_source_lists_and_gets() -> None:
    source = GmailApiMailSource(FakeGmailService(["m1", "m2"]))
    assert source.name == "gmail"
    assert source.list_ids("") == ["m1", "m2"]
    msg = source.get_message("m1")
    assert msg.subject == "SDE Intern jobs"
    assert "Acme" in msg.body_text


def test_gmail_adapter_yields_parsed_items() -> None:
    adapter = GmailAlertAdapter(GmailApiMailSource(FakeGmailService(["m1"])))
    items = adapter.fetch("")
    assert len(items) == 1
    assert items[0].source == "gmail"
    assert items[0].external_id == "m1#0"  # per-job index for digests
    assert items[0].payload["parsed"]["company"] == "Acme"
    today = adapter.cursor_after(items, "")
    assert re.fullmatch(r"\d{4}/\d{1,2}/\d{1,2}", today)
    assert adapter.cursor_after([], "2026/01/01") != ""


def test_gmail_list_ids_uses_date_cursor() -> None:
    seen: list[str] = []

    class QueryCapture(FakeGmailService):
        def list(self, **kwargs: Any) -> QueryCapture:
            seen.append(str(kwargs.get("q", "")))
            return self

    source = GmailApiMailSource(QueryCapture(["m1"]))
    source.list_ids("2026/11/20")
    assert seen and "after:2026/11/20" in seen[0]
    # Legacy message-ID cursors fall back to the default window.
    source.list_ids("m1")
    assert "after:" not in seen[-1]


class FakeMailSource(MailSource):
    def __init__(self, messages: list[MailMessage]) -> None:
        self._messages = {m.message_id: m for m in messages}

    @property
    def name(self) -> str:
        return "fake-mail"

    def list_ids(self, since: str) -> list[str]:
        ids = sorted(self._messages)
        return [i for i in ids if i > since] if since else ids

    def get_message(self, message_id: str) -> MailMessage:
        return self._messages[message_id]


def test_adapter_works_over_any_mail_source() -> None:
    source: MailSource = FakeMailSource(
        [
            MailMessage(
                message_id="a",
                sender="s",
                subject="SDE Intern jobs at Acme",
                body_text="Apply here https://x.example/1",
            )
        ]
    )
    items = GmailAlertAdapter(source).fetch("")
    assert items[0].external_id == "a#0"
    assert items[0].payload["parsed"]["company"] == "Acme"


def test_adapter_skips_unparseable_mail() -> None:
    source: MailSource = FakeMailSource(
        [MailMessage(message_id="b", sender="s", subject="Hi", body_text="x")]
    )
    assert GmailAlertAdapter(source).fetch("") == []  # no phantom posting


def test_gmail_html_only_falls_back_to_text() -> None:
    def part(mime: str, text: str) -> dict[str, Any]:
        return {
            "mimeType": mime,
            "body": {"data": base64.urlsafe_b64encode(text.encode()).decode()},
        }

    class HtmlOnly(FakeGmailService):
        def execute(self) -> dict[str, Any]:
            return {
                "id": "h1",
                "payload": {
                    "headers": [{"name": "Subject", "value": "Jobs"}],
                    "parts": [
                        part("text/html", "<p>Apply <a href='https://x.example/j'>here</a></p>")
                    ],
                },
            }

    source = GmailApiMailSource(HtmlOnly([]))
    msg = source.get_message("h1")
    assert "Apply" in msg.body_text
    assert "https://x.example/j" in msg.body_text


class FakeImapConn:
    def __init__(self, raw_by_uid: dict[str, bytes]) -> None:
        self._raw = raw_by_uid

    def login(self, *_: Any) -> None:
        pass

    def select(self, *_: Any, **__: Any) -> None:
        pass

    def uid(self, cmd: str, *args: Any) -> tuple[str, list[Any]]:
        if cmd == "SEARCH":
            return ("OK", [(" ".join(sorted(self._raw))).encode()])
        uid = str(args[0])
        return ("OK", [(uid.encode(), self._raw[uid])])

    def close(self) -> None:
        pass

    def logout(self) -> None:
        pass


def _raw_email() -> bytes:
    msg = email.message.EmailMessage()
    msg["From"] = "alert@indeed.com"
    msg["Subject"] = "SDE Intern"
    msg.set_content("Role: SDE Intern\nCompany: Beta\nApply: https://x.example/2")
    return msg.as_bytes()


def test_imap_source_readonly(monkeypatch: object) -> None:
    import imaplib

    created: list[FakeImapConn] = []

    def fake_ssl(host: str) -> FakeImapConn:
        assert host == "imap.example.com"
        conn = FakeImapConn({"7": _raw_email(), "9": _raw_email()})
        created.append(conn)
        return conn  # type: ignore[return-value]

    monkeypatch.setattr(imaplib, "IMAP4_SSL", fake_ssl)  # type: ignore[attr-defined]
    source = ImapMailSource("imap.example.com", "user", "secret")
    assert source.list_ids("") == ["7", "9"]
    assert source.list_ids("7") == ["9"]  # numeric since, not lexicographic
    msg = source.get_message("9")
    assert msg.sender == "alert@indeed.com"
    assert "Beta" in msg.body_text


def test_imap_requires_credentials() -> None:
    try:
        ImapMailSource("", "", "")
    except ValueError:
        return
    raise AssertionError("expected ValueError")


def test_source_item_defaults() -> None:
    item = SourceItem(source="gmail", payload={})
    assert item.external_id == ""
    assert item.fetched_at is not None
