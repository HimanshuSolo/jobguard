"""Digest package: builder + renderers."""

from jobguard.digest.builder import (
    Digest,
    DigestKind,
    build_digest,
    record_sent,
    render_html,
    render_text,
)

__all__ = ["Digest", "DigestKind", "build_digest", "record_sent", "render_html", "render_text"]
