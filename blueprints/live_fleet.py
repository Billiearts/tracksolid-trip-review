"""
blueprints/live_fleet.py — live Awaso↔Takoradi corridor view.

Access to the map is gated by the 'live_fleet_view' extra_role.
Editing fences and pausing/resuming the scan require an admin-level role —
either the base 'admin' role or the 'super_admin' role. A super-admin is
strictly more privileged than an admin; gating on `role == "admin"` alone
was a bug that hid the edit button from the super-admin and returned 403
on every fence mutation they attempted.

Scanning is GLOBAL:
  - Any visitor (admin or non-admin) triggers a normal live scan unless
    the global pause is on.
  - Only admins can toggle the pause via POST /live/api/scan.
  - While paused, everyone sees the same frozen snapshot.

Viewer presence:
  - GET /live/api/fleet and POST /live/api/heartbeat both record a
    heartbeat for the calling session. When no heartbeat has arrived in
    _VIEWER_TTL_SEC (60 s), get_fleet_state() returns the cached snapshot
    instead of calling Tracksolid, regardless of the admin pause state.
  - The front-end calls /live/api/heartbeat every 20 s while the tab is
    visible. This keeps the "one shared cache" alive without polling the
    full fleet endpoint more often than necessary.
"""
from __future__ import annotations

import logging
import time

from flask import (
    Blueprint, render_template, jsonify, request, session, abort, url_for,
)

from decorators import has_role
from services import audit
from services.live_fleet import (
    get_fleet_state, summarise, _load_facilities_diagnostic,
    save_fences, delete_fence, seed_default_fences,
    is_scanning, set_scanning, paused_since,
    heartbeat, viewer_count, forget_session as _forget_viewer,
)
from services.supabase_client import admin_client


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
    """Admin-level access: 'admin' OR 'super_admin'.

    `super_admin` is strictly more privileged than `admin` in this app —
    the primary super-admin is the only account that can grant/revoke
    super_admin, change any role, etc. Gating fence edits on `role ==
    'admin'` alone silently locked the super-admin out of the editor and
    of every DELETE. Accept both.
    """
    return session.get("role") in ("admin", "super_admin")


def _scan_allowed_for_this_session() -> bool:
    """Live scanning is allowed for everyone unless the global pause is on."""
    return is_scanning()


def _safe_url(endpoint: str, **values) -> str:
    """url_for() with a guard so a missing endpoint can't crash render.

    Accepts **values so callers can supply path parameters such as
    `row_id="__ID__"` for endpoints whose rule is `/api/fences/<row_id>`.
    The front-end replaces the literal `__ID__` placeholder before issuing
    the request.
    """
    try:
        return url_for(endpoint, **values)
    except Exception as exc:
        log.warning("live_fleet: url_for(%r, %r) failed: %s", endpoint, values, exc)
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
        api_delete_fence_url=_safe_url(
            "live_fleet.api_delete_fence", row_id="__ID__"
        ),
        api_heartbeat_url=_safe_url("live_fleet.api_heartbeat"),
    )


@bp.route("/api/heartbeat", methods=["POST"])
@has_role("live_fleet_view")
def api_heartbeat():
    """Lightweight presence ping. The front-end sends this every 20 s while
    the tab is visible, so the fleet endpoint can skip Tracksolid calls
    when nobody is watching."""
    sid = session.get("session_id", "")
    heartbeat(sid)
    return jsonify({
        "ok":           True,
        "viewer_count": viewer_count(),
        "scanning":     is_scanning(),
    })


@bp.route("/api/fleet")
@has_role("live_fleet_view")
def api_fleet():
    sid = session.get("session_id", "")
    if not _rate_ok(sid):
        return jsonify({
            "error": "rate_limited",
            "retry_in_sec": _RATE_LIMIT_WINDOW,
        }), 429

    # Every fleet call also registers a heartbeat, so a viewer who keeps
    # the tab open never has to wait for the heartbeat endpoint.
    heartbeat(sid)

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
    audit.record(
        "live.scan_start" if action == "start" else "live.scan_stop",
        detail=f"global scanning {'enabled' if action == 'start' else 'disabled'}",
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
        audit.record(
            "live.probe",
            detail=f"devices={len(devices)}, sample={imei}, "
                   f"location_returned={bool(result)}",
        )
        return jsonify({
            "ok": True,
            "device_count": len(devices),
            "sample_imei": imei,
            "location_returned": bool(result),
        })
    except Exception as exc:
        log.exception("live_fleet probe failed")
        audit.record(
            "live.probe",
            detail=f"failed: {type(exc).__name__}: {exc}",
        )
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
    audit.record(
        "live.seed_defaults",
        detail=f"inserted={result.get('inserted')}, skipped={result.get('skipped')}",
        meta=result,
    )
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

    # Capture per-fence before/after for the audit trail.
    names = [f.get("name") or f.get("key") or "?" for f in payload
             if isinstance(f, dict)]

    # Look up the current rows so the diff is real.
    before_by_id: dict = {}
    before_by_name: dict = {}
    try:
        current = (admin_client().table("fence_polygons")
                   .select("id,name,kind,shape,lat,lng,radius_km,polygon")
                   .execute().data or [])
        for row in current:
            if row.get("id"):
                before_by_id[row["id"]] = row
            if row.get("name"):
                before_by_name[row["name"]] = row
    except Exception as exc:
        log.warning("fence before-snapshot failed: %s", exc)

    before_fences = []
    after_fences = []
    for item in payload:
        if not isinstance(item, dict):
            continue
        row_id = item.get("id")
        name   = item.get("name") or item.get("key")
        before_row = before_by_id.get(row_id) if row_id else before_by_name.get(name)
        before_fences.append(before_row or {"_new": True, "name": name})
        after_fences.append({
            "id":         row_id,
            "name":       name,
            "kind":       item.get("kind"),
            "shape":      item.get("shape"),
            "lat":        item.get("lat"),
            "lng":        item.get("lng"),
            "radius_km":  item.get("radius_km"),
            "polygon":    item.get("polygon"),
        })

    audit.record_diff(
        "live.fence_save",
        before={"fences": before_fences},
        after={"fences": after_fences},
        detail=f"saved={result.get('saved')} fence(s): {', '.join(names)[:400]}",
        meta={
            "saved": result.get("saved"),
            "names": names,
        },
        severity="warning" if result.get("saved", 0) >= 6 else "notice",
        tags=["fence"],
    )
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
    audit.record(
        "live.fence_delete",
        target=row_id,
        detail="fence deleted",
    )
    corridor, yards, _err = _load_facilities_diagnostic()
    return jsonify({"deleted": row_id, "yards": yards, "corridor": corridor})