"""
blueprints/live_fleet.py — live Awaso↔Takoradi corridor view.

Access is gated by the 'live_fleet_view' extra_role. Admins without the
role are denied. Per-session rate limit prevents quota blowout.
"""
from __future__ import annotations

import time

from flask import Blueprint, render_template, jsonify, request, session

from decorators import has_role
from services.live_fleet import get_fleet_state, summarise


bp = Blueprint("live_fleet", __name__, url_prefix="/live")


_RATE_LIMIT_MAX    = 6
_RATE_LIMIT_WINDOW = 60.0
_rate: dict[str, list[float]] = {}


def _rate_ok(session_id: str) -> bool:
    now = time.time()
    window = _rate.setdefault(session_id, [])
    while window and (now - window[0]) > _RATE_LIMIT_WINDOW:
        window.pop(0)
    if len(window) >= _RATE_LIMIT_MAX:
        return False
    window.append(now)
    return True


@bp.route("/")
@has_role("live_fleet_view")
def index():
    return render_template("live.html")


@bp.route("/api/fleet")
@has_role("live_fleet_view")
def api_fleet():
    sid = session.get("session_id", "")
    if not _rate_ok(sid):
        return jsonify({
            "error": "rate_limited",
            "retry_in_sec": _RATE_LIMIT_WINDOW,
        }), 429

    force = request.args.get("refresh") == "1"
    states, scanning = get_fleet_state(force_refresh=force)

    return jsonify({
        "trucks":       states,
        "summary":      summarise(states),
        "scanning":     scanning,
        "generated_at": time.time(),
    })