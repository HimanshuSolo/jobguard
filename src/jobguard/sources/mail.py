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
    received_at: str = ""  # ISO-8601 UTC, when known


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

    creds = None
    if Path(token_path).exists():
        # The cached token JSON carries client_id/secret + refresh token, so
        # scheduled runs (CI) need only this file, not the client secret.
        creds = Credentials.from_authorized_user_file(token_path, [_GMAIL_READONLY_SCOPE])
    if creds and not creds.valid and creds.expired and creds.refresh_token:
        creds.refresh(Request())
        Path(token_path).write_text(creds.to_json(), encoding="utf-8")
    if not creds or not creds.valid:
        if not Path(client_secret_path).exists():
            raise FileNotFoundError(
                f"No usable Gmail token at {token_path} and no client secret at "
                f"{client_secret_path}. See README for setup; the worker skips Gmail."
            )
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


# Job mail only: known alert senders, or a hiring-ish subject (placement
# cells, company career mail). Searching the whole inbox let newsletters,
# bank mail and social notifications eat the per-run message cap.
DEFAULT_GMAIL_FILTER = (
    "-in:sent -in:chats -category:social ("
    "from:(linkedin.com OR indeed OR naukri.com OR glassdoor OR internshala OR unstop "
    "OR hirist OR instahyre OR wellfound OR cutshort OR foundit OR apna.co OR "
    "jobs-noreply OR careers OR talent OR recruit OR placement OR tnp) "
    'OR subject:(intern OR internship OR hiring OR "job alert" OR "jobs for you" OR '
    'opening OR openings OR vacancy OR "software engineer" OR developer OR sde))'
)


def _execute(request: Any) -> Any:
    """Run a Gmail API request, retrying 429/5xx with backoff when supported."""
    try:
        return request.execute(num_retries=4)
    except TypeError:  # test doubles without the num_retries keyword
        return request.execute()


class GmailApiMailSource(MailSource):
    """Gmail API source. Read-only: only ``list`` and ``get`` are used."""

    def __init__(
        self,
        service: Any,
        query: str = "",
        *,
        lookback_days: int = 3,
        max_messages: int = 300,
    ) -> None:
        self._service = service
        self._filter = query or DEFAULT_GMAIL_FILTER
        self._lookback_days = lookback_days
        self._max_messages = max_messages

    @property
    def name(self) -> str:
        return "gmail"

    def query_for(self, since: str) -> str:
        """Search string: date cursor when known, else the lookback window."""
        import re as _re

        # Legacy message-ID cursors predate date cursors: ignore them and
        # use the default window (idempotency comes from emails_seen anyway).
        if _re.fullmatch(r"\d{4}/\d{1,2}/\d{1,2}", since or ""):
            return f"after:{since} {self._filter}"
        return f"newer_than:{self._lookback_days}d {self._filter}"

    def list_ids(self, since: str) -> list[str]:
        query = self.query_for(since)
        ids: list[str] = []
        page_token: str | None = None
        while len(ids) < self._max_messages:
            kwargs: dict[str, Any] = {
                "userId": "me",
                "q": query,
                "maxResults": min(100, self._max_messages - len(ids)),
            }
            if page_token:
                kwargs["pageToken"] = page_token
            resp = _execute(self._service.users().messages().list(**kwargs))
            ids.extend(m["id"] for m in resp.get("messages", []))
            page_token = resp.get("nextPageToken")
            if not page_token:
                break
        return ids

    def get_message(self, message_id: str) -> MailMessage:
        raw = _execute(
            self._service.users().messages().get(userId="me", id=message_id, format="full")
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
        received_at = ""
        internal = str(raw.get("internalDate", "") or "")
        if internal.isdigit():
            from datetime import UTC, datetime

            received_at = datetime.fromtimestamp(int(internal) / 1000, UTC).isoformat()
        return MailMessage(
            message_id=message_id,
            received_at=received_at,
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
            received_at = ""
            try:
                from email.utils import parsedate_to_datetime

                stamp = parsedate_to_datetime(msg.get("Date", ""))
                received_at = stamp.isoformat()
            except (TypeError, ValueError, IndexError):
                pass
            return MailMessage(
                message_id=str(message_id),
                received_at=received_at,
                sender=msg.get("From", ""),
                subject=msg.get("Subject", ""),
                body_text=body_text,
                body_html=body_html,
            )


class GmailAlertAdapter:
    """Turn alert emails from any MailSource into SourceItems."""

    def __init__(self, mail_source: MailSource) -> None:
        self._mail = mail_source
        self.skipped_mail = 0  # replies / transactional mail dropped last fetch
        self.errors: list[str] = []  # per-message failures (skipped, not fatal)

    @property
    def source_name(self) -> str:
        return self._mail.name

    def fetch(self, since: str) -> list[SourceItem]:
        from jobguard.relevance import is_job_alert_mail

        items: list[SourceItem] = []
        self.skipped_mail = 0
        self.errors = []
        ids = self._mail.list_ids(since)
        for message_id in ids:
            try:
                msg = self._mail.get_message(message_id)
            except Exception as exc:
                # One unreadable message must not discard the whole sync.
                self.errors.append(f"message {message_id}: {type(exc).__name__}")
                continue
            # Company replies about your own applications ("application
            # received", "CV reviewed", interview invites) are not openings.
            if not is_job_alert_mail(msg.subject, msg.sender)[0]:
                self.skipped_mail += 1
                continue
            for index, parsed in enumerate(
                split_digest_jobs(msg.sender, msg.subject, msg.body_text)
            ):
                # A job split out of a digest gets its own card as body, not
                # the whole 20-job email (which confused LLM extraction).
                body = parsed.snippet or msg.body_text
                payload: dict[str, Any] = {
                    "sender": msg.sender,
                    "subject": msg.subject,
                    "received_at": msg.received_at,
                    "body_text": body[:8000],
                    "parsed": parsed.model_dump(),
                }
                items.append(
                    SourceItem(
                        source=self._mail.name,
                        external_id=f"{message_id}#{index}",
                        payload=payload,
                    )
                )
        if ids and len(self.errors) == len(ids):
            raise RuntimeError(f"every message failed: {self.errors[:3]}")
        return items

    def cursor_after(self, items: list[SourceItem], since: str) -> str:
        # Gmail's `after:` operator takes a date, not a message ID: the
        # cursor is the fetch date (YYYY/MM/DD).
        from datetime import UTC, datetime

        return datetime.now(UTC).strftime("%Y/%m/%d")
