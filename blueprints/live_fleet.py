"""
blueprints/live_fleet.py — live Awaso↔Takoradi corridor view.

Access to the map is gated by the 'live_fleet_view' extra_role.
Editing fences and pausing/resuming the scan require the base 'admin' role.

Scanning is GLOBAL:
  - Any visitor (admin or non-admin) triggers a normal live scan unless
    the global pause is on.
  - Only admins can toggle the pause via POST /live/api/scan.
  - While paused, everyone sees the same frozen snapshot.
"""
from __future__ import annotations

import logging
import time

from flask import (
    Blueprint, render_template, jsonify, request, session, abort, url_for,
)

from decorators import has_role
from services.live_fleet import (
    get_fleet_state, summarise, _load_facilities_diagnostic,
    save_fences, delete_fence, seed_default_fences,
    is_scanning, set_scanning, paused_since,
)


log = logging.getLogger(__name__)
bp = Blueprint("live_fleet", __name__, url_prefix="/live")


_RATE_LIMIT_MAX    = 30
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


def _scan_allowed_for_this_session() -> bool:
    """Live scanning is allowed for everyone unless the global pause is on.

    Only admins can flip the pause, but the read side is role-agnostic so a
    normal user visiting a page where no admin has scanned yet still gets
    a real scan instead of an empty map.
    """
    return is_scanning()


def _safe_url(endpoint: str) -> str:
    try:
        return url_for(endpoint)
    except Exception as exc:
        log.warning("live_fleet: url_for(%r) failed: %s", endpoint, exc)
        return ""


@bp.route("/")
@has_role("live_fleet_view")
def index():
    return render_template(
        "live.html",
        is_admin=_is_admin(),
        api_fleet_url=_safe_url("live_fleet.api_fleet"),
        api_scan_url=_safe_url("live_fleet.api_scan"),
        api_probe_url=_safe_url("live_fleet.api_probe"),
        api_seed_url=_safe_url("live_fleet.api_seed_defaults"),
        api_save_fences_url=_safe_url("live_fleet.api_save_fences"),
    )


@bp.route("/api/fleet")
@has_role("live_fleet_view")
def api_fleet():
    sid = session.get("session_id", "")
    if not _rate_ok(sid):
        return jsonify({
            "error": "rate_limited",
            "retry_in_sec": _RATE_LIMIT_WINDOW,
        }), 429

    scanning_allowed = _scan_allowed_for_this_session()
    force = (request.args.get("refresh") == "1") and scanning_allowed

    states, scanning, paused, diagnostics = get_fleet_state(
        force_refresh=force,
        session_id=sid,
        scanning_allowed=scanning_allowed,
    )

    corridor, yards, fences_error = _load_facilities_diagnostic()
    if fences_error:
        diagnostics["fences_error"] = fences_error

    # `scanning` here means "not globally paused" so the client can render
    # the same "Updated …" state for both roles.
    return jsonify({
        "trucks":           states,
        "summary":          summarise(states),
        "scanning":         scanning,
        "paused":           paused,
        "paused_since":     paused_since().strftime("%Y-%m-%d %H:%M:%S")
                            if paused and paused_since() else None,
        "can_control_scan": _is_admin(),
        "can_edit":         _is_admin(),
        "corridor":         corridor,
        "yards":            yards,
        "diagnostics":      diagnostics,
        "generated_at":     time.time(),
    })


# ---------------------------------------------------------------------------
# Scan control (admin only)
# ---------------------------------------------------------------------------

@bp.route("/api/scan", methods=["POST"])
@has_role("live_fleet_view")
def api_scan():
    if not _is_admin():
        abort(403)
    body = request.get_json(silent=True) or {}
    action = (body.get("action") or "").strip().lower()
    if action not in ("start", "stop"):
        return jsonify({"error": "action must be 'start' or 'stop'"}), 400

    set_scanning(None, action == "start")
    log.info(
        "live_fleet: admin %s set global scanning=%s",
        session.get("email"), action == "start",
    )
    return jsonify({
        "scanning": is_scanning(),
        "paused":   not is_scanning(),
    })


# ---------------------------------------------------------------------------
# Diagnostics / bootstrap (admin only)
# ---------------------------------------------------------------------------

@bp.route("/api/probe", methods=["GET"])
@has_role("live_fleet_view")
def api_probe():
    if not _is_admin():
        abort(403)
    from services.devices import get_devices
    from jimi import api_call
    try:
        devices = get_devices()
        if not devices:
            return jsonify({"ok": False, "error": "device list is empty"}), 200
        imei = devices[0]["imei"]
        r = api_call("jimi.device.location.get", {"imeis": imei})
        result = r.get("result") or r.get("data") or []
        if isinstance(result, dict):
            result = result.get("list") or []
        return jsonify({
            "ok": True,
            "device_count": len(devices),
            "sample_imei": imei,
            "location_returned": bool(result),
        })
    except Exception as exc:
        log.exception("live_fleet probe failed")
        return jsonify({"ok": False, "error": f"{type(exc).__name__}: {exc}"}), 200


@bp.route("/api/seed-defaults", methods=["POST"])
@has_role("live_fleet_view")
def api_seed_defaults():
    if not _is_admin():
        abort(403)
    try:
        result = seed_default_fences()
    except Exception as exc:
        log.exception("seed_default_fences failed")
        return jsonify({"error": f"{type(exc).__name__}: {exc}"}), 500
    corridor, yards, _err = _load_facilities_diagnostic()
    return jsonify({
        **result,
        "corridor": corridor,
        "yards":    yards,
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
    corridor, yards, _err = _load_facilities_diagnostic()
    return jsonify({"deleted": row_id, "yards": yards, "corridor": corridor})