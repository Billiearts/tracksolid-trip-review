"""
services/auth.py — auth operations against Supabase.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from services.supabase_client import anon_client, admin_client


log = logging.getLogger(__name__)


class AuthError(Exception):
    """Raised for user-facing auth problems."""


# --- helpers ---------------------------------------------------------------

def _clean(exc: Exception) -> str:
    msg = str(exc)
    for prefix in ("AuthApiError: ", "APIError: ", "AuthError: "):
        if msg.startswith(prefix):
            return msg[len(prefix):]
    return msg


def _to_iso(value) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    s = str(value)
    return s.replace("T", " ")[:19]


def _as_user_list(raw) -> list:
    if raw is None:
        return []
    if isinstance(raw, list):
        return raw
    users = getattr(raw, "users", None)
    if users is not None:
        return list(users)
    if isinstance(raw, dict):
        return raw.get("users") or []
    return []


def _default_name(email: str) -> str:
    return email.split("@", 1)[0] if email else ""


# --- public API ------------------------------------------------------------

def register(email: str, password: str, full_name: str = "") -> dict:
    """Sign up. full_name is stored in the user's metadata; the DB trigger
    copies it into profiles.full_name."""
    client = anon_client()
    payload = {
        "email": email,
        "password": password,
        "options": {"data": {"full_name": full_name.strip()}},
    }
    try:
        resp = client.auth.sign_up(payload)
    except Exception as exc:
        raise AuthError(_clean(exc)) from exc

    user = getattr(resp, "user", None)
    if user is None:
        raise AuthError("Sign-up returned no user.")

    # Backstop: if the trigger somehow didn't fire, set it manually.
    try:
        client.table("profiles").update(
            {"full_name": full_name.strip() or _default_name(email)}
        ).eq("id", user.id).execute()
    except Exception as exc:
        log.warning("could not backfill full_name for %s: %s", user.id, exc)

    return {"id": user.id, "email": user.email}


def login(email: str, password: str) -> dict:
    client = anon_client()
    try:
        resp = client.auth.sign_in_with_password({
            "email": email, "password": password,
        })
    except Exception as exc:
        raise AuthError(_clean(exc)) from exc

    user = getattr(resp, "user", None)
    if user is None:
        raise AuthError("Invalid email or password.")
    return {"id": user.id, "email": user.email}


def get_user_profile(user_id: str) -> dict:
    """Return {'role': ..., 'full_name': ...}. Defaults are safe."""
    client = admin_client()
    try:
        resp = (
            client.table("profiles")
            .select("role,full_name,email")
            .eq("id", user_id)
            .limit(1)
            .execute()
        )
    except Exception as exc:
        log.warning("get_user_profile failed for %s: %s", user_id, exc)
        return {"role": "user", "full_name": "", "email": ""}

    rows = resp.data or []
    if not rows:
        return {"role": "user", "full_name": "", "email": ""}
    row = rows[0]
    return {
        "role":      row.get("role") or "user",
        "full_name": row.get("full_name") or "",
        "email":     row.get("email") or "",
    }


def get_user_role(user_id: str) -> str:
    return get_user_profile(user_id)["role"]


def list_users_with_roles() -> list[dict]:
    client = admin_client()

    try:
        raw = client.auth.admin.list_users()
    except Exception as exc:
        log.exception("list_users failed")
        raise AuthError(_clean(exc)) from exc

    auth_users = _as_user_list(raw)

    try:
        profiles_resp = client.table("profiles").select("*").execute()
        profiles = {p["id"]: p for p in (profiles_resp.data or [])}
    except Exception as exc:
        log.warning("profiles fetch failed: %s", exc)
        profiles = {}

    out = []
    for u in auth_users:
        uid = getattr(u, "id", None)
        if not uid:
            continue
        profile = profiles.get(uid, {})
        email = getattr(u, "email", None) or ""
        out.append({
            "id":              uid,
            "email":           email,
            "full_name":       profile.get("full_name") or _default_name(email),
            "role":            profile.get("role", "user"),
            "created_at":      _to_iso(getattr(u, "created_at", None)),
            "last_sign_in_at": _to_iso(getattr(u, "last_sign_in_at", None)),
        })
    out.sort(key=lambda x: (x["role"] != "admin", x["email"] or ""))
    return out


def list_users_with_roles_paginated(page: int = 1, per_page: int = 50) -> dict:
    client = admin_client()

    try:
        raw = client.auth.admin.list_users(page=page, per_page=per_page)
    except Exception as exc:
        log.exception("list_users failed")
        raise AuthError(_clean(exc)) from exc

    auth_users = _as_user_list(raw)

    try:
        profiles_resp = client.table("profiles").select("*").execute()
        profiles = {p["id"]: p for p in (profiles_resp.data or [])}
    except Exception as exc:
        log.warning("profiles fetch failed: %s", exc)
        profiles = {}

    out = []
    for u in auth_users:
        uid = getattr(u, "id", None)
        if not uid:
            continue
        profile = profiles.get(uid, {})
        email = getattr(u, "email", None) or ""
        out.append({
            "id":              uid,
            "email":           email,
            "full_name":       profile.get("full_name") or _default_name(email),
            "role":            profile.get("role", "user"),
            "created_at":      _to_iso(getattr(u, "created_at", None)),
            "last_sign_in_at": _to_iso(getattr(u, "last_sign_in_at", None)),
        })
    out.sort(key=lambda x: (x["role"] != "admin", x["email"] or ""))
    return {"users": out, "page": page, "per_page": per_page}


def set_role(user_id: str, role: str) -> None:
    if role not in ("user", "admin"):
        raise AuthError(f"Invalid role: {role!r}")
    client = admin_client()
    try:
        client.table("profiles").update({"role": role}).eq("id", user_id).execute()
    except Exception as exc:
        raise AuthError(_clean(exc)) from exc


def create_user(email: str, password: str,
                role: str = "user", full_name: str = "") -> dict:
    if role not in ("user", "admin"):
        raise AuthError(f"Invalid role: {role!r}")
    client = admin_client()
    try:
        resp = client.auth.admin.create_user({
            "email": email,
            "password": password,
            "email_confirm": True,
            "user_metadata": {"full_name": full_name.strip()},
        })
    except Exception as exc:
        raise AuthError(_clean(exc)) from exc

    user = getattr(resp, "user", None)
    if user is None:
        raise AuthError("Create-user returned no user.")

    try:
        client.table("profiles").update({
            "role": role,
            "full_name": full_name.strip() or _default_name(email),
        }).eq("id", user.id).execute()
    except Exception as exc:
        log.warning("could not set profile for new user %s: %s", user.id, exc)

    return {"id": user.id, "email": user.email, "role": role}


def update_user(user_id: str, *,
                email: str | None = None,
                password: str | None = None,
                role: str | None = None,
                full_name: str | None = None) -> None:
    client = admin_client()

    attributes: dict = {}
    if email:
        attributes["email"] = email
        attributes["email_confirm"] = True
    if password:
        attributes["password"] = password
    if full_name is not None:
        attributes["user_metadata"] = {"full_name": full_name}

    if attributes:
        try:
            client.auth.admin.update_user_by_id(user_id, attributes)
        except Exception as exc:
            raise AuthError(_clean(exc)) from exc

    profile_update: dict = {}
    if role is not None:
        if role not in ("user", "admin"):
            raise AuthError(f"Invalid role: {role!r}")
        profile_update["role"] = role
    if full_name is not None:
        profile_update["full_name"] = full_name

    if profile_update:
        try:
            client.table("profiles").update(profile_update).eq("id", user_id).execute()
        except Exception as exc:
            raise AuthError(_clean(exc)) from exc


def delete_user(user_id: str, soft: bool = False) -> None:
    client = admin_client()
    try:
        client.auth.admin.delete_user(user_id, should_soft_delete=soft)
    except Exception as exc:
        raise AuthError(_clean(exc)) from exc