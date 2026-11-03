"""
blueprints/admin.py — admin + super-admin user management + audit trail.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from flask import (
    Blueprint, render_template, request, redirect, url_for, flash, abort,
    Response, jsonify, session,
)

from decorators import admin_required, super_admin_required
from services.auth import (
    AuthError, create_user, delete_user, get_user_profile,
    list_users_with_roles_paginated, set_role, update_user,
    PRIMARY_SUPER_ADMIN_EMAIL,
)
from services import audit as audit_svc


log = logging.getLogger(__name__)
bp = Blueprint("admin", __name__, url_prefix="/admin")


_EXTRA_ROLE_KEYS = {
    "extra_live_fleet_view": "live_fleet_view",
}


def _caller_id() -> str | None:
    return session.get("user_id")


def _caller_is_primary() -> bool:
    return bool(session.get("is_primary_super_admin"))


def _caller_is_super() -> bool:
    return session.get("role") == "super_admin"


@bp.route("/")
@admin_required
def index():
    try:
        page = max(1, int(request.args.get("page", "1")))
    except (ValueError, TypeError):
        page = 1
    try:
        result = list_users_with_roles_paginated(
            caller_user_id=_caller_id(), page=page, per_page=50,
        )
    except AuthError as exc:
        flash(str(exc), "error")
        result = {"users": [], "page": 1, "per_page": 50}
    return render_template(
        "admin/index.html",
        result=result,
        caller_is_super=_caller_is_super(),
        caller_is_primary=_caller_is_primary(),
        primary_email=PRIMARY_SUPER_ADMIN_EMAIL,
    )


@bp.route("/users/new", methods=["GET", "POST"])
@admin_required
def new_user():
    if request.method == "POST":
        full_name = (request.form.get("full_name") or "").strip()
        email     = (request.form.get("email") or "").strip()
        password  = request.form.get("password") or ""
        role      = (request.form.get("role") or "user").strip()

        if role not in ("user", "admin", "super_admin"):
            role = "user"
        # Non-primary callers can only choose user/admin.
        if role == "super_admin" and not _caller_is_primary():
            flash("Only the primary super admin can create a super admin.", "error")
            return redirect(url_for("admin.index"))

        if not email or not password:
            flash("Email and password are required.", "error")
            return render_template(
                "admin/new_user.html",
                full_name=full_name, email=email, role=role,
                caller_is_primary=_caller_is_primary(),
            )
        try:
            created = create_user(
                email=email, password=password, role=role,
                full_name=full_name, caller_user_id=_caller_id(),
            )
        except AuthError as exc:
            flash(str(exc), "error")
            return render_template(
                "admin/new_user.html",
                full_name=full_name, email=email, role=role,
                caller_is_primary=_caller_is_primary(),
            )
        audit_svc.record(
            "admin.user_create", target=email,
            detail=f"role={role}, name={full_name!r}",
            meta={"user_id": created.get("id"), "role": role},
        )
        flash(f"User {email} created.", "success")
        return redirect(url_for("admin.index"))
    return render_template(
        "admin/new_user.html",
        full_name="", email="", role="user",
        caller_is_primary=_caller_is_primary(),
    )


@bp.route("/users/<user_id>/edit", methods=["GET", "POST"])
@admin_required
def edit_user(user_id):
    profile = get_user_profile(user_id)
    if not profile.get("email"):
        flash("User not found.", "error")
        return redirect(url_for("admin.index"))

    is_primary_row = bool(profile.get("is_primary_super_admin"))
    is_self        = (user_id == _caller_id())

    # Non-primary callers cannot edit the primary row at all.
    if is_primary_row and not is_self:
        flash("This account is protected and cannot be edited.", "error")
        return redirect(url_for("admin.index"))

    user_ctx = {
        "id":                     user_id,
        "email":                  profile["email"],
        "full_name":              profile["full_name"],
        "role":                   profile["role"],
        "extra_roles":            profile.get("extra_roles") or [],
        "is_primary_super_admin": is_primary_row,
    }

    if request.method == "POST":
        full_name = (request.form.get("full_name") or "").strip()
        email     = (request.form.get("email") or "").strip()
        password  = request.form.get("password") or ""
        role      = (request.form.get("role") or "").strip() or None

        new_extra_roles: list[str] = []
        for form_key, role_value in _EXTRA_ROLE_KEYS.items():
            if request.form.get(form_key) == "on":
                new_extra_roles.append(role_value)

        if role is not None and role not in ("user", "admin", "super_admin"):
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
                caller_user_id=_caller_id(),
            )
        except AuthError as exc:
            flash(str(exc), "error")
            return redirect(url_for("admin.edit_user", user_id=user_id))

        changes = []
        if email != profile.get("email"):
            changes.append(f"email {profile.get('email')!r}→{email!r}")
        if password:
            changes.append("password reset")
        if role and role != profile.get("role"):
            changes.append(f"role {profile.get('role')!r}→{role!r}")
        if full_name != profile.get("full_name"):
            changes.append(f"name {profile.get('full_name')!r}→{full_name!r}")
        old_extra = set(profile.get("extra_roles") or [])
        new_extra = set(new_extra_roles)
        if old_extra != new_extra:
            changes.append(
                f"extra_roles {sorted(old_extra)}→{sorted(new_extra)}"
            )
        if password:
            audit_svc.record(
                "auth.password_change", target=email,
                detail="admin forced password reset",
                meta={"user_id": user_id, "forced_by_admin": True},
            )
        if changes:
            audit_svc.record(
                "admin.user_update", target=email,
                detail="; ".join(changes),
                meta={"user_id": user_id},
            )

        flash("User updated.", "success")
        return redirect(url_for("admin.index"))

    return render_template(
        "admin/edit_user.html",
        user=user_ctx,
        caller_is_primary=_caller_is_primary(),
        is_self=is_self,
    )


@bp.route("/users/<user_id>/delete", methods=["POST"])
@admin_required
def delete_user_route(user_id):
    soft = request.form.get("soft", "0") == "1"
    profile = get_user_profile(user_id)
    email = profile.get("email") or user_id

    if profile.get("is_primary_super_admin"):
        flash("This account is protected and cannot be deleted.", "error")
        return redirect(url_for("admin.index"))

    try:
        delete_user(user_id, soft=soft, caller_user_id=_caller_id())
    except AuthError as exc:
        flash(str(exc), "error")
        return redirect(url_for("admin.index"))

    audit_svc.record(
        "admin.user_delete", target=email,
        detail="soft" if soft else "hard",
        meta={"user_id": user_id, "soft": soft},
    )
    flash("User deleted.", "success")
    return redirect(url_for("admin.index"))


@bp.route("/users/<user_id>/role", methods=["POST"])
@admin_required
def change_role(user_id):
    role = (request.form.get("role") or "").strip()
    profile = get_user_profile(user_id)
    email = profile.get("email") or user_id

    if profile.get("is_primary_super_admin"):
        flash("This account is protected and cannot have its role changed.",
              "error")
        return redirect(url_for("admin.index"))

    try:
        set_role(user_id, role, caller_user_id=_caller_id())
    except AuthError as exc:
        flash(str(exc), "error")
        return redirect(url_for("admin.index"))

    audit_svc.record(
        "admin.role_change", target=email,
        detail=f"{profile.get('role')!r} → {role!r}",
        meta={"user_id": user_id, "old_role": profile.get("role"),
              "new_role": role},
    )
    return redirect(url_for("admin.index"))
# ---------------------------------------------------------------------------
# Audit trail — SUPER ADMIN ONLY
# ---------------------------------------------------------------------------

@bp.route("/audit")
@super_admin_required
def audit():
    try:
        page = max(1, int(request.args.get("page", "1")))
    except (ValueError, TypeError):
        page = 1

    f_event    = (request.args.get("event")    or "").strip() or None
    f_actor    = (request.args.get("actor")    or "").strip() or None
    f_target   = (request.args.get("target")   or "").strip() or None
    f_severity = (request.args.get("severity") or "").strip() or None
    f_outcome  = (request.args.get("outcome")  or "").strip() or None
    f_req      = (request.args.get("request_id") or "").strip() or None
    f_days_raw = (request.args.get("days")     or "").strip()
    try:
        f_days = int(f_days_raw) if f_days_raw else None
    except ValueError:
        f_days = None

    try:
        result = audit_svc.list_events(
            page=page, per_page=100,
            event=f_event, actor_email=f_actor, target=f_target,
            severity=f_severity, outcome=f_outcome,
            request_id=f_req, days=f_days,
        )
    except Exception as exc:
        log.exception("audit list failed")
        flash(f"Could not load audit trail: {exc}", "error")
        result = {"rows": [], "page": 1, "per_page": 100, "has_more": False}

    return render_template(
        "admin/audit.html",
        result=result,
        events=audit_svc.distinct_events(),
        severities=audit_svc.distinct_severities(),
        f_event=f_event or "",
        f_actor=f_actor or "",
        f_target=f_target or "",
        f_severity=f_severity or "",
        f_outcome=f_outcome or "",
        f_request_id=f_req or "",
        f_days=f_days_raw,
        unacked_alerts=audit_svc.unacked_alert_count(),
    )


@bp.route("/audit/verify")
@super_admin_required
def audit_verify():
    result = audit_svc.verify_chain(limit=50000)
    audit_svc.record(
        "audit.verify",
        severity=("critical" if not result.get("ok") else "info"),
        outcome=("failure" if not result.get("ok") else "success"),
        detail=(f"checked={result.get('checked')}, "
                f"ok={result.get('ok')}, "
                f"first_mismatch={result.get('first_mismatch')}"),
        meta=result,
    )
    if result.get("ok"):
        flash(f"Chain OK — {result.get('checked', 0)} rows verified.",
              "success")
    else:
        flash(f"Chain BROKEN at row {result.get('first_mismatch')} "
              f"(checked {result.get('checked', 0)} rows).", "error")
    return redirect(url_for("admin.audit"))


@bp.route("/audit/export")
@super_admin_required
def audit_export():
    fmt = (request.args.get("format") or "csv").lower()
    if fmt not in ("csv", "jsonl"):
        fmt = "csv"
    try:
        days = int(request.args.get("days") or "90")
    except ValueError:
        days = 90
    days = max(1, min(3650, days))

    mimetype, body = audit_svc.export_rows(fmt=fmt, days=days)
    audit_svc.record(
        "audit.export", severity="notice",
        detail=f"format={fmt}, days={days}, bytes={len(body)}",
        tags=["export", "audit"],
    )
    filename = (f"audit-"
                f"{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}"
                f".{fmt}")
    return Response(
        body, mimetype=mimetype,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@bp.route("/audit/<row_id>")
@super_admin_required
def audit_detail(row_id):
    try:
        resp = (audit_svc._client().table("audit_log")
                .select("*")
                .eq("id", row_id)
                .limit(1)
                .execute())
        rows = resp.data or []
        if not rows:
            return jsonify({"error": "not found"}), 404
        r = rows[0]
        return jsonify({
            "id": r.get("id"), "event": r.get("event"),
            "severity": r.get("severity"), "outcome": r.get("outcome"),
            "actor_id": r.get("actor_id"),
            "actor_email": r.get("actor_email"),
            "target": r.get("target"), "detail": r.get("detail"),
            "meta": r.get("meta") or {},
            "before": r.get("before_json"),
            "after": r.get("after_json"),
            "diff": r.get("diff_json") or {},
            "tags": r.get("tags") or [],
            "ip": r.get("ip"), "user_agent": r.get("user_agent"),
            "request_id": r.get("request_id"),
            "route": r.get("route"), "method": r.get("method"),
            "path": r.get("path"),
            "status_code": r.get("status_code"),
            "duration_ms": r.get("duration_ms"),
            "redacted": r.get("redacted"),
            "created_at": r.get("created_at"),
        })
    except Exception as exc:
        log.exception("audit_detail failed")
        return jsonify({"error": f"{type(exc).__name__}: {exc}"}), 500


@bp.route("/audit/correlate/<request_id>")
@super_admin_required
def audit_correlate(request_id):
    rows = audit_svc.correlation_rows(request_id)
    return jsonify({"request_id": request_id, "rows": rows})


# ---------------------------------------------------------------------------
# Alerts — SUPER ADMIN ONLY
# ---------------------------------------------------------------------------

@bp.route("/alerts")
@super_admin_required
def alerts():
    try:
        page = max(1, int(request.args.get("page", "1")))
    except (ValueError, TypeError):
        page = 1
    only_unacked = request.args.get("unacked") == "1"
    try:
        result = audit_svc.list_alerts(
            page=page, per_page=50, only_unacked=only_unacked,
        )
    except Exception as exc:
        log.exception("alerts list failed")
        flash(f"Could not load alerts: {exc}", "error")
        result = {"rows": [], "page": 1, "per_page": 50, "has_more": False}
    return render_template(
        "admin/alerts.html",
        result=result,
        only_unacked=only_unacked,
        unacked_count=audit_svc.unacked_alert_count(),
    )


@bp.route("/alerts/<alert_id>/ack", methods=["POST"])
@super_admin_required
def alert_ack(alert_id):
    audit_svc.acknowledge_alert(
        alert_id,
        user_id=session.get("user_id"),
        email=session.get("email"),
    )
    flash("Alert acknowledged.", "success")
    return redirect(url_for("admin.alerts"))