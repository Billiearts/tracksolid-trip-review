"""
blueprints/auto_release_admin.py — small admin-only surface for the
auto-release worker.

Routes
------
  GET  /admin/auto-release           status page + sweep button
  POST /admin/auto-release/sweep     run sweep_once() immediately
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from flask import (
    Blueprint, render_template, redirect, url_for, flash, request, abort,
)
from decorators import admin_required
from services import auto_release
from services import audit


log = logging.getLogger(__name__)
bp = Blueprint("auto_release_admin", __name__,
               url_prefix="/admin/auto-release")


@bp.route("/")
@admin_required
def index():
    return render_template(
        "admin/auto_release.html",
        last=auto_release.last_sweep(),
        interval=auto_release.SWEEP_INTERVAL_SECONDS,
        release_groups=sorted(auto_release.RELEASE_GROUPS),
    )


@bp.route("/sweep", methods=["POST"])
@admin_required
def sweep():
    try:
        result = auto_release.force_sweep_now()
    except Exception as exc:
        log.exception("manual sweep failed")
        flash(f"Sweep failed: {exc}", "error")
        return redirect(url_for("auto_release_admin.index"))

    audit.record(
        "admin.auto_release_sweep",
        target="auto-release",
        detail=(f"open={result.get('open_assignments')} "
                f"released={result.get('released')} "
                f"errors={result.get('errors')}"),
        meta=result,
        severity="notice" if result.get("released", 0) else "info",
    )
    flash(
        f"Sweep done — released {result.get('released', 0)} of "
        f"{result.get('open_assignments', 0)} open assignment(s).",
        "success",
    )
    return redirect(url_for("auto_release_admin.index"))