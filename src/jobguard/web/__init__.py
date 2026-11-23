"""Dashboard: FastAPI app, auth, demo seed."""

from jobguard.web.app import create_app
from jobguard.web.auth import (
    CSRF_COOKIE,
    SESSION_COOKIE,
    create_session,
    hash_password,
    new_csrf_token,
    verify_password,
    verify_session,
)
from jobguard.web.seed import ensure_demo_approval, seed_demo

__all__ = [
    "CSRF_COOKIE",
    "SESSION_COOKIE",
    "create_app",
    "create_session",
    "ensure_demo_approval",
    "hash_password",
    "new_csrf_token",
    "seed_demo",
    "verify_password",
    "verify_session",
]
