"""
blueprints/auth.py — sign-in, sign-up, sign-out, password reset.
"""
from __future__ import annotations

import logging

from flask import (
    Blueprint, render_template, request, redirect, url_for,
    session, flash,
)

from config import SUPABASE_URL, SUPABASE_ANON_KEY
from services import sessions as sessions_svc
from services.auth import (
    AuthError, get_user_profile, login as auth_login,
    register as auth_register,
    send_password_reset as auth_send_password_reset,
    update_password_with_token as auth_update_password_with_token,
)


log = logging.getLogger(__name__)
bp = Blueprint("auth", __name__)


def _auth_render(template: str, **kwargs):
    """Render an auth template with Supabase config always available.

    Guards against ``{{ supabase_url | tojson }}`` raising
    ``TypeError: Object of type Undefined is not JSON serializable``
    if a template references it without the view passing it.
    """
    kwargs.setdefault("supabase_url", SUPABASE_URL)
    kwargs.setdefault("supabase_anon_key", SUPABASE_ANON_KEY)
    return render_template(template, **kwargs)


@bp.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        email    = (request.form.get("email") or "").strip()
        password = request.form.get("password") or ""
        if not email or not password:
            flash("Email and password are required.", "error")
            return _auth_render("auth/login.html")

        try:
            user = auth_login(email, password)
        except AuthError as exc:
            flash(str(exc), "error")
            return _auth_render("auth/login.html")

        profile = get_user_profile(user["id"])
        try:
            session_id = sessions_svc.create(
                user_id=user["id"],
                email=user["email"],
                role=profile["role"],
                full_name=profile.get("full_name") or "",
                extra_roles=profile.get("extra_roles") or [],
            )
        except Exception as exc:
            log.exception("could not create session")
            flash(f"Could not sign in: {exc}", "error")
            return _auth_render("auth/login.html")

        session.clear()
        session["session_id"]  = session_id
        session["user_id"]     = user["id"]
        session["email"]       = user["email"]
        session["role"]        = profile["role"]
        session["full_name"]   = profile.get("full_name") or ""
        session["extra_roles"] = profile.get("extra_roles") or []

        nxt = request.args.get("next")
        if nxt and nxt.startswith("/") and not nxt.startswith("//"):
            return redirect(nxt)
        return redirect(url_for("index"))

    return _auth_render("auth/login.html")


@bp.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "POST":
        full_name = (request.form.get("full_name") or "").strip()
        email     = (request.form.get("email") or "").strip()
        password  = request.form.get("password") or ""
        password2 = request.form.get("password2") or ""

        if not email or not password:
            flash("Email and password are required.", "error")
            return _auth_render("auth/register.html",
                                full_name=full_name, email=email)
        if password != password2:
            flash("Passwords do not match.", "error")
            return _auth_render("auth/register.html",
                                full_name=full_name, email=email)
        if len(password) < 8:
            flash("Password must be at least 8 characters.", "error")
            return _auth_render("auth/register.html",
                                full_name=full_name, email=email)

        try:
            auth_register(email, password, full_name)
        except AuthError as exc:
            flash(str(exc), "error")
            return _auth_render("auth/register.html",
                                full_name=full_name, email=email)
        flash("Account created. Please sign in.", "success")
        return redirect(url_for("auth.login"))

    return _auth_render("auth/register.html", full_name="", email="")


@bp.route("/logout")
def logout():
    sid = session.get("session_id")
    if sid:
        sessions_svc.delete(sid)
    session.clear()
    return redirect(url_for("auth.login"))


@bp.route("/forgot-password", methods=["GET", "POST"])
def forgot_password():
    if request.method == "POST":
        email = (request.form.get("email") or "").strip()
        if not email:
            flash("Please enter your email address.", "error")
            return _auth_render("auth/forgot_password.html", email=email)

        # Supabase will redirect the user back here with the recovery
        # token. The reset_password.html page reads it and stashes it
        # in a hidden field for the POST.
        redirect_to = url_for("auth.reset_password", _external=True)

        try:
            auth_send_password_reset(email, redirect_to=redirect_to)
        except AuthError as exc:
            # Should not raise for unknown emails, but if it does, don't
            # leak existence — just log it.
            log.warning("password reset request failed: %s", exc)

        flash(
            "If that email is registered, a reset link is on its way.",
            "success",
        )
        return redirect(url_for("auth.login"))

    return _auth_render("auth/forgot_password.html", email="")


@bp.route("/reset-password", methods=["GET", "POST"])
def reset_password():
    if request.method == "GET":
        return _auth_render(
            "auth/reset_password.html",
            access_token=request.args.get("access_token") or "",
        )

    access_token = (request.form.get("access_token") or "").strip()
    password     = request.form.get("password") or ""
    confirm      = request.form.get("confirm") or ""

    if not access_token:
        flash("This reset link is invalid or has expired.", "error")
        return redirect(url_for("auth.forgot_password"))
    if len(password) < 8:
        flash("Password must be at least 8 characters.", "error")
        return _auth_render("auth/reset_password.html",
                            access_token=access_token)
    if password != confirm:
        flash("Passwords do not match.", "error")
        return _auth_render("auth/reset_password.html",
                            access_token=access_token)

    try:
        auth_update_password_with_token(access_token, password)
    except AuthError as exc:
        flash(str(exc), "error")
        return _auth_render("auth/reset_password.html",
                            access_token=access_token)

    flash("Password updated. Please sign in.", "success")
    return redirect(url_for("auth.login"))