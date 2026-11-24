"""Clerk sign-in for the dashboard (server-side session verification).

Flow: the browser loads clerk-js from the instance's Frontend API, which
signs the user in and keeps a short-lived session JWT in the ``__session``
cookie. Every request verifies that JWT here: RS256 signature against the
instance JWKS, issuer, expiry, optional ``azp`` origin allowlist.

A valid Clerk session is NOT enough. This is a single-owner dashboard, so
the user must also be allowlisted, by Clerk user id or by verified email
(looked up with the secret key). With no allowlist possible, access fails
closed. Anyone can create an account on a Clerk app whose sign-ups are open,
and they must not see your jobs or resume.
"""

from __future__ import annotations

import base64
import re
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Literal

import httpx
import jwt

from jobguard.config import Settings

SESSION_COOKIE_PREFIX = "__session"
_HOST_RE = re.compile(r"^[a-z0-9.-]+\.[a-z]{2,}$")
_USER_CACHE_TTL_S = 600.0

UserLookup = Callable[[str], list[str]]


class ClerkConfigError(ValueError):
    """The publishable key does not encode a Frontend API host."""


def frontend_api_from_key(publishable_key: str) -> str:
    """Decode ``pk_test_<base64("host$")>`` to the Frontend API host."""
    parts = publishable_key.strip().split("_", 2)
    if len(parts) != 3 or parts[0] != "pk" or parts[1] not in ("test", "live"):
        raise ClerkConfigError("CLERK_PUBLISHABLE_KEY must look like pk_test_... or pk_live_...")
    encoded = parts[2]
    try:
        decoded = base64.b64decode(encoded + "=" * (-len(encoded) % 4)).decode("utf-8")
    except (ValueError, UnicodeDecodeError) as exc:
        raise ClerkConfigError("CLERK_PUBLISHABLE_KEY is not valid base64") from exc
    host = decoded.rstrip("$").strip().casefold()
    if not _HOST_RE.match(host):
        raise ClerkConfigError("CLERK_PUBLISHABLE_KEY does not encode a host name")
    return host


def _csv(value: str) -> frozenset[str]:
    return frozenset(v.strip() for v in value.split(",") if v.strip())


@dataclass(frozen=True)
class ClerkConfig:
    publishable_key: str
    frontend_api: str
    secret_key: str = ""
    allowed_emails: frozenset[str] = frozenset()
    allowed_user_ids: frozenset[str] = frozenset()
    authorized_parties: frozenset[str] = frozenset()

    @classmethod
    def from_settings(cls, settings: Settings) -> ClerkConfig | None:
        """None when Clerk is not configured (legacy password login stays on)."""
        if not settings.clerk_publishable_key.strip():
            return None
        emails = _csv(settings.clerk_allowed_emails) or _csv(settings.owner_email)
        return cls(
            publishable_key=settings.clerk_publishable_key.strip(),
            frontend_api=frontend_api_from_key(settings.clerk_publishable_key),
            secret_key=settings.clerk_secret_key.strip(),
            allowed_emails=frozenset(e.casefold() for e in emails),
            allowed_user_ids=_csv(settings.clerk_allowed_user_ids),
            authorized_parties=frozenset(
                p.rstrip("/") for p in _csv(settings.clerk_authorized_parties)
            ),
        )

    @property
    def issuer(self) -> str:
        return f"https://{self.frontend_api}"

    @property
    def jwks_url(self) -> str:
        return f"{self.issuer}/.well-known/jwks.json"

    @property
    def script_url(self) -> str:
        # Clerk Core 3: clerk-js v6. v5 is now refused as outdated by Clerk.
        return f"{self.issuer}/npm/@clerk/clerk-js@6/dist/clerk.browser.js"

    @property
    def ui_script_url(self) -> str:
        # Core 3 ships the prebuilt UI (sign-in/up components) separately; it
        # defines window.__internal_ClerkUICtor, passed to Clerk.load().
        return f"{self.issuer}/npm/@clerk/ui@1/dist/ui.browser.js"

    def csp_sources(self) -> dict[str, str]:
        """Extra CSP sources clerk-js needs (per Clerk's CSP guide)."""
        return {
            "script-src": f"{self.issuer} https://challenges.cloudflare.com",
            "connect-src": self.issuer,
            "img-src": "https://img.clerk.com",
            "frame-src": "https://challenges.cloudflare.com",
            "worker-src": "blob:",
        }


@dataclass(frozen=True)
class ClerkResult:
    status: Literal["ok", "signed_out", "forbidden"]
    user_id: str = ""
    email: str = ""
    reason: str = ""


@dataclass
class ClerkAuth:
    """Verify session tokens and apply the owner allowlist."""

    config: ClerkConfig
    signing_key: Callable[[str], Any] | None = None  # token -> public key (tests inject)
    user_lookup: UserLookup | None = None  # user id -> verified emails (tests inject)
    leeway_s: float = 5.0
    _cache: dict[str, tuple[float, list[str]]] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock)
    _jwks: jwt.PyJWKClient | None = None

    def _key_for(self, token: str) -> Any:
        if self.signing_key is not None:
            return self.signing_key(token)
        if self._jwks is None:
            self._jwks = jwt.PyJWKClient(
                self.config.jwks_url,
                cache_keys=True,
                lifespan=3600,
                headers={"User-Agent": "JobGuard/0.1"},
                timeout=10,
            )
        return self._jwks.get_signing_key_from_jwt(token).key

    def _emails(self, user_id: str) -> list[str]:
        now = time.monotonic()
        with self._lock:
            cached = self._cache.get(user_id)
            if cached and cached[0] > now:
                return cached[1]
        lookup = self.user_lookup or self._lookup_via_api
        emails = [e.casefold() for e in lookup(user_id)]
        with self._lock:
            self._cache[user_id] = (now + _USER_CACHE_TTL_S, emails)
        return emails

    def _lookup_via_api(self, user_id: str) -> list[str]:
        if not self.config.secret_key or not re.fullmatch(r"user_[A-Za-z0-9]+", user_id):
            return []
        resp = httpx.get(
            f"https://api.clerk.com/v1/users/{user_id}",
            headers={"Authorization": f"Bearer {self.config.secret_key}"},
            timeout=10.0,
        )
        if resp.status_code != 200:
            return []
        data = resp.json()
        return [
            str(entry.get("email_address", ""))
            for entry in data.get("email_addresses", []) or []
            if isinstance(entry, dict)
            and (entry.get("verification") or {}).get("status") == "verified"
        ]

    def verify(self, token: str) -> ClerkResult:
        if not token:
            return ClerkResult("signed_out", reason="no session")
        try:
            claims = jwt.decode(
                token,
                self._key_for(token),
                algorithms=["RS256"],
                issuer=self.config.issuer,
                leeway=self.leeway_s,
                options={"require": ["exp", "iat", "iss", "sub"]},
            )
        except jwt.ExpiredSignatureError:
            return ClerkResult("signed_out", reason="session token expired")
        except (jwt.PyJWTError, httpx.HTTPError, OSError) as exc:
            return ClerkResult("signed_out", reason=f"invalid session ({type(exc).__name__})")
        user_id = str(claims.get("sub", ""))
        parties = self.config.authorized_parties
        if parties and str(claims.get("azp", "")).rstrip("/") not in parties:
            return ClerkResult("forbidden", user_id, reason="token issued for another origin")
        if user_id in self.config.allowed_user_ids:
            return ClerkResult("ok", user_id)
        if not self.config.secret_key:
            return ClerkResult(
                "forbidden",
                user_id,
                reason="set CLERK_SECRET_KEY (email allowlist) or add this user id "
                "to CLERK_ALLOWED_USER_IDS",
            )
        try:
            emails = self._emails(user_id)
        except (httpx.HTTPError, ValueError) as exc:
            return ClerkResult(
                "forbidden", user_id, reason=f"user lookup failed ({type(exc).__name__})"
            )
        match = next((e for e in emails if e in self.config.allowed_emails), "")
        if match:
            return ClerkResult("ok", user_id, email=match)
        return ClerkResult("forbidden", user_id, reason="this account is not allowed")

    def verify_cookies(self, cookies: dict[str, str]) -> ClerkResult:
        """Try ``__session`` then suffixed ``__session_*`` cookies."""
        names = [SESSION_COOKIE_PREFIX] + sorted(
            n for n in cookies if n.startswith(SESSION_COOKIE_PREFIX + "_")
        )
        best = ClerkResult("signed_out", reason="no session")
        for name in names:
            result = self.verify(cookies.get(name, ""))
            if result.status == "ok":
                return result
            if result.status == "forbidden":
                best = result
        return best
