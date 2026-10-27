"""
blueprints/admin.py — admin-only user management.
"""
from __future__ import annotations

import logging

from flask import (
    Blueprint, render_template, request, redirect, url_for, flash, abort,
)

from decorators import admin_required
from services.auth import (
    AuthError, create_user, delete_user, get_user_profile,
    list_users_with_roles_paginated, set_role, update_user,
)


log = logging.getLogger(__name__)
bp = Blueprint("admin", __name__, url_prefix="/admin")


_EXTRA_ROLE_KEYS = {
    "extra_live_fleet_view": "live_fleet_view",
}


@bp.route("/")
@admin_required
def index():
    try:
        page = max(1, int(request.args.get("page", "1")))
    except (ValueError, TypeError):
        page = 1
    try:
        result = list_users_with_roles_paginated(page=page, per_page=50)
    except AuthError as exc:
        flash(str(exc), "error")
        result = {"users": [], "page": 1, "per_page": 50}
    return render_template("admin/index.html", result=result)


@bp.route("/users/new", methods=["GET", "POST"])
@admin_required
def new_user():
    if request.method == "POST":
        full_name = (request.form.get("full_name") or "").strip()
        email     = (request.form.get("email") or "").strip()
        password  = request.form.get("password") or ""
        role      = (request.form.get("role") or "user").strip()
        if role not in ("user", "admin"):
            role = "user"
        if not email or not password:
            flash("Email and password are required.", "error")
            return render_template("admin/new_user.html",
                                   full_name=full_name, email=email, role=role)
        try:
            create_user(email=email, password=password,
                        role=role, full_name=full_name)
        except AuthError as exc:
            flash(str(exc), "error")
            return render_template("admin/new_user.html",
                                   full_name=full_name, email=email, role=role)
        flash(f"User {email} created.", "success")
        return redirect(url_for("admin.index"))
    return render_template("admin/new_user.html",
                           full_name="", email="", role="user")


@bp.route("/users/<user_id>/edit", methods=["GET", "POST"])
@admin_required
def edit_user(user_id):
    profile = get_user_profile(user_id)
    if not profile.get("email"):
        flash("User not found.", "error")
        return redirect(url_for("admin.index"))

    user_ctx = {
        "id":          user_id,
        "email":       profile["email"],
        "full_name":   profile["full_name"],
        "role":        profile["role"],
        "extra_roles": profile.get("extra_roles") or [],
    }

    if request.method == "POST":
        full_name = (request.form.get("full_name") or "").strip()
        email     = (request.form.get("email") or "").strip()
        password  = request.form.get("password") or ""
        role      = (request.form.get("role") or "user").strip()

        new_extra_roles: list[str] = []
        for form_key, role_value in _EXTRA_ROLE_KEYS.items():
            if request.form.get(form_key) == "on":
                new_extra_roles.append(role_value)

        if role not in ("user", "admin"):
            flash("Invalid role.", "error")
            return redirect(url_for("admin.edit_user", user_id=user_id))
        if not email:
            flash("Email is required.", "error")
            return redirect(url_for("admin.edit_user", user_id=user_id))

        try:
            update_user(
                user_id,
                email=email if email != profile.get("email") else None,
                password=password or None,
                role=role,
                full_name=full_name,
                extra_roles=new_extra_roles,
            )
        except AuthError as exc:
            flash(str(exc), "error")
            return redirect(url_for("admin.edit_user", user_id=user_id))

        flash("User updated.", "success")
        return redirect(url_for("admin.index"))

    return render_template("admin/edit_user.html", user=user_ctx)


@bp.route("/users/<user_id>/delete", methods=["POST"])
@admin_required
def delete_user_route(user_id):
    soft = request.form.get("soft", "0") == "1"
    try:
        delete_user(user_id, soft=soft)
    except AuthError as exc:
        flash(str(exc), "error")
        return redirect(url_for("admin.index"))
    flash("User deleted.", "success")
    return redirect(url_for("admin.index"))


@bp.route("/users/<user_id>/role", methods=["POST"])
@admin_required
def change_role(user_id):
    role = (request.form.get("role") or "").strip()
    try:
        set_role(user_id, role)
    except AuthError as exc:
        flash(str(exc), "error")
    return redirect(url_for("admin.index"))