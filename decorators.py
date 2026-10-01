"""
decorators.py — auth guards for Flask routes.

Session shape (set by blueprints/auth.py):
  session['session_id'] -> uuid string from user_sessions table
  session['user_id']    -> cached for convenience
  session['email']      -> cached
  session['role']       -> cached
  session['full_name']  -> cached

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


def login_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        record = _current_session()
        if record is None:
            session.clear()
            return redirect(url_for("auth.login", next=request.path))
        session["user_id"]   = record["user_id"]
        session["email"]     = record["email"]
        session["role"]      = record["role"]
        session["full_name"] = record["full_name"]
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
        session["user_id"]   = record["user_id"]
        session["email"]     = record["email"]
        session["role"]      = record["role"]
        session["full_name"] = record["full_name"]
        return f(*args, **kwargs)
    return wrapper