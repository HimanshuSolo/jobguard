"""Source adapters: mail, boards, RSS, and shared HTTP/HTML helpers."""

from jobguard.sources.base import SourceAdapter, SourceItem
from jobguard.sources.boards import BOARD_DOMAINS, BoardFeedAdapter, BoardSpec, load_boards
from jobguard.sources.configure import adapter_status, build_adapters, build_prefilter
from jobguard.sources.digest_split import (
    split_digest_jobs,
    split_indeed_jobs,
    split_linkedin_jobs,
)
from jobguard.sources.html_clean import extract_text, html_to_markdown
from jobguard.sources.http import (
    FetchBlockedIp,
    FetchDenied,
    FetchFailed,
    FetchTooLarge,
    fetch_url_safe,
)
from jobguard.sources.mail import (
    GmailAlertAdapter,
    GmailApiMailSource,
    ImapMailSource,
    MailMessage,
    MailSource,
    build_gmail_service,
)
from jobguard.sources.parsers import AlertPosting, detect_platform, parse_alert
from jobguard.sources.rss import RssAdapter, RssFeed, parse_feeds_setting

__all__ = [
    "BOARD_DOMAINS",
    "AlertPosting",
    "BoardFeedAdapter",
    "BoardSpec",
    "FetchBlockedIp",
    "FetchDenied",
    "FetchFailed",
    "FetchTooLarge",
    "GmailAlertAdapter",
    "GmailApiMailSource",
    "adapter_status",
    "build_adapters",
    "build_prefilter",
    "ImapMailSource",
    "MailMessage",
    "MailSource",
    "RssAdapter",
    "RssFeed",
    "SourceAdapter",
    "SourceItem",
    "build_gmail_service",
    "detect_platform",
    "extract_text",
    "fetch_url_safe",
    "html_to_markdown",
    "load_boards",
    "parse_alert",
    "parse_feeds_setting",
    "split_digest_jobs",
    "split_indeed_jobs",
    "split_linkedin_jobs",
]
