"""
blueprints/admin.py — admin-only user management.
"""
from __future__ import annotations
from services import sessions as sessions_svc
from flask import (
    Blueprint, render_template, request, redirect, url_for, flash,
)

from decorators import admin_required
from services.auth import (
    list_users_with_roles_paginated,
    create_user, update_user, delete_user, set_role,
    AuthError,
)


bp = Blueprint("admin", __name__, url_prefix="/admin")


@bp.route("/")
@admin_required
def index():
    page     = max(1, int(request.args.get("page", 1)))
    per_page = 50
    try:
        result = list_users_with_roles_paginated(page=page, per_page=per_page)
    except AuthError as exc:
        flash(f"Could not load users: {exc}", "error")
        result = {"users": [], "page": 1, "per_page": per_page}
    return render_template("admin/index.html", result=result)


@bp.route("/users/new", methods=["GET", "POST"])
@admin_required
def new_user():
    if request.method == "POST":
        full_name = (request.form.get("full_name") or "").strip()
        email     = (request.form.get("email") or "").strip()
        password  = request.form.get("password") or ""
        role      = request.form.get("role") or "user"

        if not full_name or not email or len(password) < 8:
            flash("Name and email required; password must be at least 8 chars.",
                  "error")
            return render_template("admin/new_user.html",
                                   email=email, role=role, full_name=full_name)

        try:
            create_user(email, password, role, full_name)
            flash(f"User {full_name} created.", "success")
            return redirect(url_for("admin.index"))
        except AuthError as exc:
            flash(f"Could not create user: {exc}", "error")

    return render_template("admin/new_user.html")


@bp.route("/users/<user_id>/edit", methods=["GET", "POST"])
@admin_required
def edit_user(user_id):
    try:
        result = list_users_with_roles_paginated(page=1, per_page=1000)
        user = next((u for u in result["users"] if u["id"] == user_id), None)
    except AuthError as exc:
        flash(f"Could not load user: {exc}", "error")
        return redirect(url_for("admin.index"))

    if user is None:
        flash("User not found.", "error")
        return redirect(url_for("admin.index"))

    if request.method == "POST":
        full_name = (request.form.get("full_name") or "").strip()
        email     = (request.form.get("email") or "").strip()
        password  = request.form.get("password") or ""
        role      = request.form.get("role") or "user"

        try:
            update_user(
                user_id,
                email=email if email != user["email"] else None,
                password=password or None,
                role=role,
                full_name=full_name,
            )
            sessions_svc.delete_for_user(user_id)
            flash("User updated.", "success")
            return redirect(url_for("admin.index"))
        except AuthError as exc:
            flash(f"Could not update user: {exc}", "error")

    return render_template("admin/edit_user.html", user=user)


@bp.route("/users/<user_id>/delete", methods=["POST"])
@admin_required
def delete_user_route(user_id):
    soft = request.form.get("soft") == "1"
    try:
        delete_user(user_id, soft=soft)
        flash("User deleted." + (" (soft)" if soft else ""), "success")
    except AuthError as exc:
        flash(f"Could not delete user: {exc}", "error")
    return redirect(url_for("admin.index"))


@bp.route("/users/<user_id>/role", methods=["POST"])
@admin_required
def change_role(user_id):
    role = request.form.get("role", "")
    try:
        set_role(user_id, role)
        flash(f"Role updated to {role}.", "success")
    except AuthError as exc:
        flash(f"Could not update role: {exc}", "error")
    return redirect(url_for("admin.index"))