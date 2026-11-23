"""Notifier interface. Sending uses SEPARATE credentials from mail reading."""

from __future__ import annotations

from typing import Protocol


class Notifier(Protocol):
    """Deliver a message. Implementations never choose the recipient freely."""

    @property
    def name(self) -> str: ...

    def send(self, *, to: str, subject: str, html: str, text: str) -> None:
        """Send one message; raise on refusal or transport failure."""
        ...
