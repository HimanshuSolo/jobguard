"""Dashboard auth: single-user login, HMAC sessions, double-submit CSRF.

Personal single-user tool: the password is stored as a SHA-256 hex digest in
`DASHBOARD_PASSWORD_HASH` (set it from a strong random password; no two-user
or reset flows by design). Sessions are stateless HMAC cookies; CSRF uses the
double-submit cookie pattern so no server-side session store is needed.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import time

SESSION_COOKIE = "jg_session"
CSRF_COOKIE = "jg_csrf"
SESSION_TTL_S = 86400


def hash_password(password: str) -> str:
    """SHA-256 hex of a password (generate offline, store the digest)."""
    return hashlib.sha256(password.encode()).hexdigest()


def verify_password(password: str, expected_hash: str) -> bool:
    """Constant-time password check; False when no hash is configured."""
    if not expected_hash:
        return False
    return hmac.compare_digest(hash_password(password), expected_hash.lower())


def _sig(secret: str, payload: str) -> str:
    return hmac.new(secret.encode(), payload.encode(), hashlib.sha256).hexdigest()


def create_session(username: str, secret: str) -> str:
    """Mint a session cookie value `user.ts.sig`."""
    stamp = str(int(time.time()))
    payload = f"{username}.{stamp}"
    return f"{payload}.{_sig(secret, payload)}"


def verify_session(value: str, username: str, secret: str) -> bool:
    """Validate signature, owner, and TTL."""
    try:
        user, stamp, sig = value.split(".")
    except ValueError:
        return False
    if user != username or not hmac.compare_digest(sig, _sig(secret, f"{user}.{stamp}")):
        return False
    try:
        age = time.time() - int(stamp)
    except ValueError:
        return False
    return 0 <= age <= SESSION_TTL_S


def new_csrf_token() -> str:
    """Random per-login CSRF token (double-submit cookie pattern)."""
    return secrets.token_urlsafe(16)
