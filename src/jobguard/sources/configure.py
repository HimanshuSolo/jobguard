"""Shared adapter construction for the CLI worker and the dashboard sync button."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

from jobguard.config import Settings
from jobguard.sources.base import SourceAdapter
from jobguard.sources.boards import BoardFeedAdapter, load_boards

if TYPE_CHECKING:
    from jobguard.ingestion import Prefilter
from jobguard.sources.mail import GmailAlertAdapter, GmailApiMailSource, ImapMailSource
from jobguard.sources.rss import RssAdapter, parse_feeds_setting


def build_adapters(settings: Settings) -> tuple[list[SourceAdapter], list[str]]:
    """Build every configured source adapter; returns (adapters, skipped_reasons)."""
    adapters: list[SourceAdapter] = []
    skipped: list[str] = []

    boards = load_boards(settings.boards_config_path)
    if boards:
        adapters.append(BoardFeedAdapter(boards=boards))
    elif Path(settings.boards_config_path).exists():
        skipped.append(f"No valid boards in {settings.boards_config_path}; skipping.")
    else:
        skipped.append(
            f"No {settings.boards_config_path}; skipping boards (see data/boards.example.yaml)."
        )

    feeds, _ = parse_feeds_setting(settings.rss_feeds)
    if feeds:
        from urllib.parse import urlparse

        feed_domains = list({urlparse(f.url).hostname or "" for f in feeds} - {""})
        adapters.append(RssAdapter(feeds=feeds, allowed_domains=feed_domains))

    if settings.gmail_client_secret and Path(settings.gmail_client_secret).exists():
        try:
            from jobguard.sources import build_gmail_service

            service = build_gmail_service(settings.gmail_client_secret, settings.gmail_token_path)
            adapters.append(GmailAlertAdapter(GmailApiMailSource(service)))
        except FileNotFoundError as exc:
            skipped.append(f"Gmail skipped: {exc}")
    else:
        skipped.append(
            "Gmail skipped: no client secret file "
            "(set GMAIL_CLIENT_SECRET=data/private/gmail_client_secret.json)."
        )

    if (
        settings.imap_host
        and settings.imap_password_file
        and Path(settings.imap_password_file).exists()
    ):
        password = Path(settings.imap_password_file).read_text(encoding="utf-8").strip()
        adapters.append(
            GmailAlertAdapter(ImapMailSource(settings.imap_host, settings.imap_user, password))
        )
    elif settings.imap_host:
        skipped.append("IMAP skipped: password file missing.")

    return adapters, skipped


def build_prefilter(settings: Settings) -> Prefilter:
    """Prefilter from settings (shared by CLI and web)."""
    # Local import: jobguard.ingestion (transitively) imports jobguard.sources,
    # so an eager import here would be circular.
    from jobguard.ingestion import Prefilter

    return Prefilter.from_settings(settings.prefilter_keywords, settings.prefilter_senders)


def adapter_status(settings: Settings) -> list[dict[str, Any]]:
    """Dashboard-friendly adapter inventory with configuration state."""
    adapters, skipped = build_adapters(settings)
    return [{"name": a.source_name, "configured": True, "note": ""} for a in adapters] + [
        {"name": "unconfigured", "configured": False, "note": note} for note in skipped
    ]
