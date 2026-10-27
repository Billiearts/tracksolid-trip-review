"""
decorators.py — auth guards for Flask routes.

Session shape (set by blueprints/auth.py):
  session['session_id']  -> uuid string from user_sessions table
  session['user_id']     -> cached for convenience
  session['email']       -> cached
  session['role']        -> cached
  session['full_name']   -> cached
  session['extra_roles'] -> cached list

Every request verifies the session row still exists in Postgres.
If not, the cookie is cleared and the user is bounced to /login.
"""
from functools import wraps

from flask import session, redirect, url_for, request, abort

from services import sessions as sessions_svc


def _current_session():
    sid = session.get("session_id")
    if not sid:
        return None
    return sessions_svc.load(sid)


def _refresh_session_from_record(record: dict) -> None:
    session["user_id"]     = record["user_id"]
    session["email"]       = record["email"]
    session["role"]        = record["role"]
    session["full_name"]   = record["full_name"]
    session["extra_roles"] = record.get("extra_roles") or []


def login_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        record = _current_session()
        if record is None:
            session.clear()
            return redirect(url_for("auth.login", next=request.path))
        _refresh_session_from_record(record)
        return f(*args, **kwargs)
    return wrapper


def admin_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        record = _current_session()
        if record is None:
            session.clear()
            return redirect(url_for("auth.login", next=request.path))
        if record["role"] != "admin":
            abort(403)
        _refresh_session_from_record(record)
        return f(*args, **kwargs)
    return wrapper


def has_role(role_name: str):
    """Require a specific extra_role on top of any base role.

    Admins do NOT automatically pass this — the role must be present in
    profiles.extra_roles. Use for capabilities that must be granted
    independently of the admin role.
    """
    def decorator(f):
        @wraps(f)
        def wrapper(*args, **kwargs):
            record = _current_session()
            if record is None:
                session.clear()
                return redirect(url_for("auth.login", next=request.path))

            # Refresh the extra_roles from profiles on every call so
            # grant / revoke takes effect without a re-login.
            from services.auth import get_user_profile
            profile = get_user_profile(record["user_id"])
            extra_roles = profile.get("extra_roles") or []

            if role_name not in extra_roles:
                abort(403)

            _refresh_session_from_record(record)
            session["extra_roles"] = extra_roles
            return f(*args, **kwargs)
        return wrapper
    return decorator