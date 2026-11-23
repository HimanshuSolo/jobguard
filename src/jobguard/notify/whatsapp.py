"""STUB (optional future): WhatsApp Cloud API notifier.

Clearly marked stub per SPEC: WhatsApp is an optional future channel, not
part of any current pipeline. Instantiating or calling `send` raises so it
can never silently become a live path.
"""

from __future__ import annotations


class WhatsAppNotifier:
    """Not implemented. Raises on any use."""

    @property
    def name(self) -> str:
        return "whatsapp-stub"

    def send(self, *, to: str, subject: str, html: str, text: str) -> None:
        """Always raises: WhatsApp delivery is not implemented (stub)."""
        raise NotImplementedError("WhatsAppNotifier is a stub for an optional future channel.")
