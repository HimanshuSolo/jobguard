"""Approval flow: signed, expiring, single-use tokens (Phase 5).

- Only token *hashes* touch the DB (`PendingAction.token_hash`, SHA-256).
- Tokens are random (`secrets`); nothing is derived from the plan content.
- `verify_token` returns the action only while it is pending and unexpired.
- `decide` transitions pending -> approved/rejected exactly once (single-use:
  a second decision attempt raises); execution runs only for approved,
  still-valid actions through a registered executor, and everything is
  audited.
- The web endpoint (Phase 6) shows a confirmation page on GET and acts on
  POST only, so mail link scanners cannot trigger execution.
"""

from __future__ import annotations

import hashlib
import secrets
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy.orm import Session

from jobguard.store.models import AuditLog, PendingAction

Executor = Callable[[dict[str, Any]], str]


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def create_action(
    session: Session,
    *,
    plan: dict[str, Any],
    risk: str,
    expires_hours: int = 72,
) -> tuple[PendingAction, str]:
    """Store a pending plan; return (row, one-time token shown to the owner)."""
    token = secrets.token_urlsafe(32)
    action = PendingAction(
        plan=dict(plan),
        risk=risk,
        status="pending",
        token_hash=_hash(token),
        expires_at=datetime.now(UTC) + timedelta(hours=expires_hours),
    )
    session.add(action)
    session.commit()
    _audit(session, "approval.created", {"action_id": action.id, "risk": risk})
    session.commit()
    return action, token


def verify_token(session: Session, token: str) -> PendingAction | None:
    """Return the action iff it is pending and unexpired; else None."""
    action = session.query(PendingAction).filter_by(token_hash=_hash(token)).first()
    if action is None or action.status != "pending":
        return None
    expires = action.expires_at
    aware = expires if expires.tzinfo else expires.replace(tzinfo=UTC)
    if aware <= datetime.now(UTC):
        return None
    return action


class ApprovalError(RuntimeError):
    """Invalid, expired, or already-decided approval."""


def decide(
    session: Session,
    action: PendingAction,
    *,
    approved: bool,
    executor: Executor | None = None,
) -> str:
    """Approve/reject exactly once; execute approved plans via `executor`."""
    fresh = session.get(PendingAction, action.id)
    if fresh is None or fresh.status != "pending":
        raise ApprovalError("action is no longer pending")
    expires = fresh.expires_at
    aware = expires if expires.tzinfo else expires.replace(tzinfo=UTC)
    if aware <= datetime.now(UTC):
        raise ApprovalError("action expired")
    result = ""
    if approved:
        fresh.status = "approved"
        if executor is not None:
            result = executor(dict(fresh.plan or {}))
    else:
        fresh.status = "rejected"
    fresh.decided_at = datetime.now(UTC)
    _audit(
        session,
        "approval.decided",
        {
            "action_id": fresh.id,
            "approved": approved,
            "risk": fresh.risk,
            "executed": bool(result),
        },
    )
    session.commit()
    return result


def _audit(session: Session, event: str, detail: dict[str, Any]) -> None:
    session.add(AuditLog(event=event, detail=detail))
