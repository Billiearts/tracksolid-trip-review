"""
blueprints/live_fleet.py — live Awaso↔Takoradi corridor view.

Access to the map is gated by the 'live_fleet_view' extra_role.
Editing fences additionally requires the base 'admin' role.
Per-session rate limit prevents quota blowout.
"""
from __future__ import annotations

import logging
import time

from flask import (
    Blueprint, render_template, jsonify, request, session, abort,
)

from decorators import has_role
from services.live_fleet import (
    get_fleet_state, summarise, _load_facilities,
    save_fences, delete_fence,
)


log = logging.getLogger(__name__)
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


def _is_admin() -> bool:
    return session.get("role") == "admin"


@bp.route("/")
@has_role("live_fleet_view")
def index():
    return render_template("live.html", is_admin=_is_admin())


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

    # Include both corridor and yard fences so the map can draw everything
    # from the DB (rather than hardcoding Awaso/Takoradi in the template).
    corridor, yards = _load_facilities()

    return jsonify({
        "trucks":       states,
        "summary":      summarise(states),
        "scanning":     scanning,
        "corridor":     corridor,
        "yards":        yards,
        "can_edit":     _is_admin(),
        "generated_at": time.time(),
    })


# ---------------------------------------------------------------------------
# Editing (admin only)
# ---------------------------------------------------------------------------

def _require_admin():
    if not _is_admin():
        abort(403)


@bp.route("/api/fences", methods=["POST"])
@has_role("live_fleet_view")
def api_save_fences():
    _require_admin()
    payload = request.get_json(silent=True)
    if not isinstance(payload, list):
        return jsonify({"error": "expected a JSON list of fences"}), 400
    try:
        result = save_fences(payload)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    except Exception as exc:
        log.exception("save_fences failed")
        return jsonify({"error": f"{type(exc).__name__}: {exc}"}), 500
    return jsonify(result)


@bp.route("/api/fences/<row_id>", methods=["DELETE"])
@has_role("live_fleet_view")
def api_delete_fence(row_id):
    _require_admin()
    try:
        ok = delete_fence(row_id)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    except Exception as exc:
        log.exception("delete_fence failed")
        return jsonify({"error": f"{type(exc).__name__}: {exc}"}), 500
    if not ok:
        return jsonify({"error": "not found"}), 404
    corridor, yards = _load_facilities()
    return jsonify({"deleted": row_id, "yards": yards, "corridor": corridor})