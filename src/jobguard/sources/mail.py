"""Mailbox access: Gmail (readonly) + IMAP read-only fallback.

Hard rules enforced here, not by convention:
- Gmail uses the ``gmail.readonly`` scope exclusively; this module has no code
  path that sends, deletes, or modifies mail.
- The IMAP fallback opens the mailbox with ``readonly=True`` and never issues
  STORE/EXPUNGE. The service/inbox objects are injected, so unit tests use
  fakes and no test touches a real mailbox.
"""

from __future__ import annotations

import base64
import email
import imaplib
from collections.abc import Iterator
from contextlib import contextmanager
from email.message import Message
from typing import Any, Protocol

from pydantic import BaseModel

from jobguard.sources.base import SourceItem
from jobguard.sources.digest_split import split_digest_jobs
from jobguard.sources.html_clean import extract_text

_GMAIL_READONLY_SCOPE = "https://www.googleapis.com/auth/gmail.readonly"


class MailMessage(BaseModel):
    """One mailbox message (content is untrusted data)."""

    message_id: str
    sender: str = ""
    subject: str = ""
    body_text: str = ""
    body_html: str = ""


class MailSource(Protocol):
    """Read-only mailbox behind one interface (Gmail API or IMAP)."""

    @property
    def name(self) -> str: ...

    def list_ids(self, since: str) -> list[str]:
        """IDs newer than ``since`` (empty ``since`` = latest page only)."""
        ...

    def get_message(self, message_id: str) -> MailMessage: ...


def build_gmail_service(client_secret_path: str, token_path: str) -> Any:
    """Build an OAuth Gmail service with the readonly scope only.

    Refresh tokens live in ``token_path`` (under ``data/private/``). Raises
    ``FileNotFoundError`` with a setup pointer when credentials are absent —
    the caller (worker) then skips Gmail instead of crashing.
    """
    from pathlib import Path

    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow
    from googleapiclient.discovery import build

    if not Path(client_secret_path).exists():
        raise FileNotFoundError(
            f"Gmail client secret not found at {client_secret_path}. "
            "See README for setup; the worker skips Gmail until it exists."
        )
    creds = None
    if Path(token_path).exists():
        creds = Credentials.from_authorized_user_file(token_path, [_GMAIL_READONLY_SCOPE])
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(
                client_secret_path, [_GMAIL_READONLY_SCOPE]
            )
            creds = flow.run_local_server(port=0)
        Path(token_path).parent.mkdir(parents=True, exist_ok=True)
        Path(token_path).write_text(creds.to_json(), encoding="utf-8")
    return build("gmail", "v1", credentials=creds)


def _decode_part(part: Message[str, str]) -> str:
    """Decode one MIME part to text (untrusted content, errors replaced)."""
    payload = part.get_payload(decode=True)
    if isinstance(payload, str):
        return payload
    if not isinstance(payload, bytes):
        return ""
    charset = part.get_content_charset() or "utf-8"
    return payload.decode(charset, errors="replace")


def _gmail_part_text(part: dict[str, Any], mime: str) -> str:
    if part.get("mimeType", "").startswith(mime):
        data = (part.get("body") or {}).get("data", "")
        if data:
            return base64.urlsafe_b64decode(data).decode("utf-8", errors="replace")
    for sub in part.get("parts", []):
        text = _gmail_part_text(sub, mime)
        if text:
            return text
    return ""


class GmailApiMailSource(MailSource):
    """Gmail API source. Read-only: only ``list`` and ``get`` are used."""

    def __init__(self, service: Any, query: str = "newer_than:2d") -> None:
        self._service = service
        self._query = query

    @property
    def name(self) -> str:
        return "gmail"

    def list_ids(self, since: str) -> list[str]:
        import re as _re

        query = self._query
        if _re.fullmatch(r"\d{4}/\d{1,2}/\d{1,2}", since or ""):
            query = f"{self._query} after:{since}"
        # Legacy message-ID cursors predate date cursors: ignore them and
        # use the default window (idempotency comes from emails_seen anyway).
        resp = self._service.users().messages().list(userId="me", q=query, maxResults=100).execute()
        return [m["id"] for m in resp.get("messages", [])]

    def get_message(self, message_id: str) -> MailMessage:
        raw = (
            self._service.users()
            .messages()
            .get(userId="me", id=message_id, format="full")
            .execute()
        )
        headers = {
            h["name"].lower(): h.get("value", "") for h in raw.get("payload", {}).get("headers", [])
        }
        payload = raw.get("payload", {})
        body_text = _gmail_part_text(payload, "text/plain")
        body_html = _gmail_part_text(payload, "text/html")
        if not body_text and body_html:
            # HTML-only mail (common for alerts): fall back to text with links.
            from jobguard.sources.html_clean import html_to_markdown

            body_text = html_to_markdown(body_html)
        return MailMessage(
            message_id=message_id,
            sender=headers.get("from", ""),
            subject=headers.get("subject", ""),
            body_text=body_text,
            body_html=body_html,
        )


class ImapMailSource(MailSource):
    """IMAP fallback. Opens the inbox readonly; never writes flags."""

    def __init__(self, host: str, user: str, password: str, mailbox: str = "INBOX") -> None:
        if not host or not user or not password:
            raise ValueError("IMAP host/user/password are all required")
        self._host = host
        self._user = user
        self._password = password
        self._mailbox = mailbox

    @property
    def name(self) -> str:
        return "imap"

    @contextmanager
    def _connect(self) -> Iterator[imaplib.IMAP4_SSL]:
        conn = imaplib.IMAP4_SSL(self._host)
        try:
            conn.login(self._user, self._password)
            conn.select(self._mailbox, readonly=True)
            yield conn
        finally:
            try:
                conn.close()
            except imaplib.IMAP4.error:
                pass
            conn.logout()

    def list_ids(self, since: str) -> list[str]:
        # ``since`` is the last seen UID; compare numerically, not as strings.
        with self._connect() as conn:
            _, data = conn.uid("SEARCH", "CHARSET", "UTF-8", "ALL")
            uids = data[0].split() if data and data[0] else []
            texts = [u.decode() for u in uids]
            if since:
                texts = [u for u in texts if int(u) > int(since)]
            return texts[-100:]

    def get_message(self, message_id: str) -> MailMessage:
        with self._connect() as conn:
            _, data = conn.uid("FETCH", message_id, "(RFC822)")
            raw = data[0][1] if data and data[0] else b""
            msg = email.message_from_bytes(raw if isinstance(raw, bytes) else b"")
            body_text, body_html = "", ""
            if msg.is_multipart():
                for part in msg.walk():
                    if part.is_multipart():
                        continue
                    text = _decode_part(part)
                    if part.get_content_type() == "text/plain" and not body_text:
                        body_text = text
                    elif part.get_content_type() == "text/html" and not body_html:
                        body_html = text
            elif msg.get_content_type() == "text/html":
                body_html = _decode_part(msg)
            else:
                body_text = _decode_part(msg)
            if not body_text and body_html:
                body_text = extract_text(body_html)
            return MailMessage(
                message_id=str(message_id),
                sender=msg.get("From", ""),
                subject=msg.get("Subject", ""),
                body_text=body_text,
                body_html=body_html,
            )


class GmailAlertAdapter:
    """Turn alert emails from any MailSource into SourceItems."""

    def __init__(self, mail_source: MailSource) -> None:
        self._mail = mail_source

    @property
    def source_name(self) -> str:
        return self._mail.name

    def fetch(self, since: str) -> list[SourceItem]:
        items: list[SourceItem] = []
        for message_id in self._mail.list_ids(since):
            msg = self._mail.get_message(message_id)
            for index, parsed in enumerate(
                split_digest_jobs(msg.sender, msg.subject, msg.body_text)
            ):
                payload: dict[str, Any] = {
                    "sender": msg.sender,
                    "subject": msg.subject,
                    "body_text": msg.body_text[:8000],
                    "parsed": parsed.model_dump(),
                }
                items.append(
                    SourceItem(
                        source=self._mail.name,
                        external_id=f"{message_id}#{index}",
                        payload=payload,
                    )
                )
        return items

    def cursor_after(self, items: list[SourceItem], since: str) -> str:
        # Gmail's `after:` operator takes a date, not a message ID: the
        # cursor is the fetch date (YYYY/MM/DD).
        from datetime import UTC, datetime

        return datetime.now(UTC).strftime("%Y/%m/%d")
