"""
blueprints/auth.py — login, register, logout.
"""
from __future__ import annotations

from flask import (
    Blueprint, render_template, request, redirect, url_for, session, flash,
)

from services.auth import (
    register as sb_register, login as sb_login,
    get_user_profile, AuthError,
)
from services import sessions as sessions_svc


bp = Blueprint("auth", __name__)


@bp.route("/login", methods=["GET", "POST"])
def login():
    if session.get("session_id"):
        return redirect(url_for("index"))

    if request.method == "POST":
        email    = (request.form.get("email") or "").strip()
        password = request.form.get("password") or ""
        try:
            user    = sb_login(email, password)
            profile = get_user_profile(user["id"])
            display_name = profile["full_name"] or user["email"].split("@")[0]

            sid = sessions_svc.create(
                user_id=user["id"],
                email=user["email"],
                role=profile["role"],
                full_name=display_name,
            )
            session.clear()
            session["session_id"] = sid
            session["user_id"]    = user["id"]
            session["email"]      = user["email"]
            session["role"]       = profile["role"]
            session["full_name"]  = display_name

            nxt = request.args.get("next")
            return redirect(nxt or url_for("index"))
        except AuthError as exc:
            flash(f"Sign-in failed: {exc}", "error")
        except Exception as exc:
            flash(f"Sign-in failed: {exc}", "error")

    return render_template("auth/login.html")


@bp.route("/register", methods=["GET", "POST"])
def register():
    if session.get("session_id"):
        return redirect(url_for("index"))

    if request.method == "POST":
        full_name = (request.form.get("full_name") or "").strip()
        email     = (request.form.get("email") or "").strip()
        password  = request.form.get("password") or ""
        password2 = request.form.get("password2") or ""

        def fail(msg):
            flash(msg, "error")
            return render_template("auth/register.html",
                                   email=email, full_name=full_name)

        if not full_name:
            return fail("Please enter your name.")
        if password != password2:
            return fail("Passwords don't match.")
        if len(password) < 8:
            return fail("Password must be at least 8 characters.")

        try:
            sb_register(email, password, full_name)
            flash("Account created. Please sign in.", "success")
            return redirect(url_for("auth.login"))
        except AuthError as exc:
            flash(f"Registration failed: {exc}", "error")

    return render_template("auth/register.html")


@bp.route("/logout")
def logout():
    sid = session.get("session_id")
    if sid:
        sessions_svc.delete(sid)
    session.clear()
    return redirect(url_for("auth.login"))