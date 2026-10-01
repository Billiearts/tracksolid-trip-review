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

    @bp.route("/forgot-password", methods=["GET", "POST"])
    def forgot_password():
     if session.get("session_id"):
        return redirect(url_for("index"))

    if request.method == "POST":
        email = (request.form.get("email") or "").strip()
        if not email:
            flash("Please enter your email.", "error")
            return render_template("auth/forgot_password.html", email=email)

        try:
            from services.supabase_client import anon_client
            client = anon_client()
            # Supabase Auth's built-in reset. Sends an email with a link
            # pointing to /reset-password (configured in Supabase dashboard).
            client.auth.reset_password_for_email(
                email,
                options={"redirect_to": url_for("auth.reset_password", _external=True)},
            )
            # Always show success — Supabase doesn't reveal whether the
            # email exists (prevents user enumeration).
            flash(
                "If an account exists for that email, a reset link has been sent. "
                "Check your inbox (and spam folder).",
                "success",
            )
            return redirect(url_for("auth.login"))
        except Exception as exc:
            flash(f"Could not send reset email: {exc}", "error")

    return render_template("auth/forgot_password.html")


@bp.route("/reset-password", methods=["GET", "POST"])
def reset_password():
    # Supabase redirects here with a recovery token in the URL fragment
    # (#access_token=...&type=recovery). The JS on this page reads it,
    # then calls updateUser() with the new password.
    if request.method == "POST":
        new_password = request.form.get("password") or ""
        confirm      = request.form.get("confirm") or ""
        access_token = request.form.get("access_token") or ""

        if len(new_password) < 8:
            flash("Password must be at least 8 characters.", "error")
            return render_template("auth/reset_password.html",
                                   access_token=access_token)
        if new_password != confirm:
            flash("Passwords don't match.", "error")
            return render_template("auth/reset_password.html",
                                   access_token=access_token)
        if not access_token:
            flash("Reset link is invalid or expired. Please request a new one.",
                  "error")
            return redirect(url_for("auth.forgot_password"))

        try:
            from services.supabase_client import anon_client
            client = anon_client()
            # Set the session using the recovery token, then update the user.
            client.auth.set_session(access_token, "")
            client.auth.update_user({"password": new_password})
            client.auth.sign_out()
            flash("Password updated. Please sign in with your new password.",
                  "success")
            return redirect(url_for("auth.login"))
        except Exception as exc:
            flash(f"Could not reset password: {exc}", "error")

    return render_template("auth/reset_password.html", access_token="")