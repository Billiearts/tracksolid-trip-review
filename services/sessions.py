"""
services/sessions.py — Postgres-backed user sessions.

Every request that passes @login_required calls `load(sid)`, which
updates `last_seen_at` on the row. That makes `user_sessions.last_seen_at`
the de-facto "last activity" timestamp for every active user.

`last_seen_for_users(user_ids)` returns one row per user with the most
recent `last_seen_at` across all their sessions — used by the admin page
as the "Last login" column.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from services.supabase_client import admin_client


log = logging.getLogger(__name__)


_SESSION_TTL_DAYS = 30


def _now() -> datetime:
    return datetime.now(timezone.utc)


def create(*, user_id: str, email: str, role: str,
           full_name: str, extra_roles: list[str]) -> str:
    client = admin_client()
    now = _now()
    expires = now + timedelta(days=_SESSION_TTL_DAYS)
    payload = {
        "user_id":       user_id,
        "email":         email,
        "role":          role,
        "full_name":     full_name,
        "extra_roles":   extra_roles or [],
        "created_at":    now.isoformat(),
        "last_seen_at":  now.isoformat(),
        "expires_at":    expires.isoformat(),
    }
    resp = client.table("user_sessions").insert(payload).execute()
    rows = resp.data or []
    if not rows:
        raise RuntimeError("session create: no row returned")
    return rows[0]["id"]


def load(session_id: str) -> dict | None:
    if not session_id:
        return None
    client = admin_client()
    try:
        resp = (client.table("user_sessions")
                .select("*")
                .eq("id", session_id)
                .limit(1)
                .execute())
    except Exception:
        log.exception("session load failed for %s", session_id)
        return None

    rows = resp.data or []
    if not rows:
        return None
    row = rows[0]

    expires_at = row.get("expires_at")
    if expires_at:
        try:
            exp = datetime.fromisoformat(str(expires_at).replace("Z", "+00:00"))
            if exp < _now():
                delete(session_id)
                return None
        except ValueError:
            pass

    # Bump last_seen_at so the admin page's Last login column reflects
    # real activity, not just the original sign-in.
    try:
        client.table("user_sessions").update({
            "last_seen_at": _now().isoformat(),
        }).eq("id", session_id).execute()
    except Exception:
        log.debug("session load: last_seen bump failed for %s", session_id)

    return row


def delete(session_id: str) -> None:
    if not session_id:
        return
    try:
        admin_client().table("user_sessions").delete().eq(
            "id", session_id).execute()
    except Exception:
        log.exception("session delete failed for %s", session_id)


def purge_expired() -> int:
    cutoff = _now().isoformat()
    try:
        resp = (admin_client().table("user_sessions")
                .delete()
                .lt("expires_at", cutoff)
                .execute())
        return len(resp.data or [])
    except Exception:
        log.exception("session purge failed")
        return 0


def last_seen_for_users(user_ids: list[str]) -> dict[str, datetime]:
    """Return {user_id: last_seen_at} for the given user IDs.

    One query, aggregated client-side (Supabase doesn't expose MAX +
    GROUP BY through the PostgREST client without an RPC). For a page of
    50 users this is trivial.
    """
    ids = [u for u in (user_ids or []) if u]
    if not ids:
        return {}

    try:
        resp = (admin_client().table("user_sessions")
                .select("user_id,last_seen_at")
                .in_("user_id", ids)
                .order("last_seen_at", desc=True)
                .execute())
    except Exception:
        log.exception("last_seen_for_users failed")
        return {}

    out: dict[str, datetime] = {}
    for row in (resp.data or []):
        uid = row.get("user_id")
        raw = row.get("last_seen_at")
        if not uid or not raw:
            continue
        if uid in out:
            # Already have the latest for this user; the ORDER BY
            # desc ensures the first hit per uid is the max.
            continue
        try:
            out[uid] = datetime.fromisoformat(
                str(raw).replace("Z", "+00:00")
            )
        except ValueError:
            continue
    return out