"""
blueprints/probe.py — admin-only truck state probe.

Routes
------
  GET  /admin/probe/                 search page
  GET  /admin/probe/state?q=<query>  JSON payload for the probe result

The `q` parameter is either a truck name (e.g. "GT 8573 - 25") or an
IMEI (e.g. "865135061393573"). We resolve either form to an IMEI using
the device cache, then run `fleet_state.current_state()`.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from flask import (
    Blueprint, render_template, request, jsonify, abort,
)

from decorators import admin_required
from services import fleet_state
from services import auto_release
from services import drivers as drivers_svc
from services import devices as devices_svc
from services.supabase_client import admin_client
from jimi import fetch_alarms_chunked


log = logging.getLogger(__name__)
bp = Blueprint("probe", __name__, url_prefix="/admin/probe")


def _resolve_to_imei(q: str) -> tuple[str | None, str | None]:
    """Return (imei, truck_name) if q matches a device, else (None, None).

    q may be an IMEI or a truck display name (exact match, then
    case-insensitive substring match).
    """
    q = (q or "").strip()
    if not q:
        return None, None

    try:
        devs = devices_svc.get_devices_cached_only() or devices_svc.get_devices()
    except Exception:
        log.exception("probe: device list unavailable")
        return None, None

    # Exact IMEI match
    for d in devs or []:
        if (d.get("imei") or "").strip() == q:
            return d["imei"], (d.get("deviceName") or "").strip()

    # Exact device name match
    q_lower = q.lower()
    for d in devs or []:
        if (d.get("deviceName") or "").strip().lower() == q_lower:
            return d["imei"], d["deviceName"].strip()

    # Substring match on name
    for d in devs or []:
        if q_lower in (d.get("deviceName") or "").lower():
            return d["imei"], d["deviceName"].strip()

    return None, None


def _open_assignment_for_imei(imei: str) -> dict | None:
    try:
        resp = (admin_client().table("truck_assignments")
                .select("id,driver_id,truck_id,imei,assigned_at")
                .eq("imei", imei)
                .is_("released_at", "null")
                .limit(1)
                .execute())
        rows = resp.data or []
        return rows[0] if rows else None
    except Exception:
        log.exception("probe: assignment lookup failed")
        return None


def _recent_fence_events(imei: str, n: int = 10) -> list[dict]:
    end = datetime.now(timezone.utc)
    begin = end - timedelta(hours=48)
    try:
        alarms = fetch_alarms_chunked(imei, begin, end) or []
    except Exception as exc:
        return [{"error": str(exc)}]

    rows = []
    for a in alarms:
        kind = fleet_state._event_kind(a)
        if not kind:
            continue
        name = fleet_state._fence_name(a)
        geoid = fleet_state._fence_geoid(a)
        if not name and not geoid:
            continue
        when = fleet_state._parse_ts(
            a.get("alertTime") or a.get("alarmTime")
            or a.get("alarm_time") or a.get("time") or a.get("gpsTime")
        )
        if when is None:
            continue
        rows.append({
            "at":     when.isoformat(),
            "kind":   kind,
            "name":   name,
            "geoid":  geoid,
        })
    rows.sort(key=lambda r: r["at"], reverse=True)
    return rows[:n]


@bp.route("/")
@admin_required
def index():
    return render_template("admin/probe.html")


@bp.route("/state")
@admin_required
def state():
    q = (request.args.get("q") or "").strip()
    if not q:
        return jsonify({"error": "missing_query",
                        "message": "Type a truck name or IMEI."}), 400

    imei, name = _resolve_to_imei(q)
    if not imei:
        return jsonify({"error": "not_found",
                        "message": f"No truck matches {q!r}."}), 404

    # 1. current_state
    try:
        st = fleet_state.current_state(imei)
    except Exception as exc:
        log.exception("probe: current_state failed")
        return jsonify({"error": "state_failed",
                        "message": str(exc)}), 500

    if st is None:
        return jsonify({
            "imei":        imei,
            "truck_id":    name,
            "state":       None,
            "would_release": False,
            "message":     "Tracksolid lookup failed. Try again in a moment.",
        })

    # 2. would the sweep release
    group = st.get("group")
    would_release = group in auto_release.RELEASE_GROUPS

    # 3. recent fence events
    events = _recent_fence_events(imei)

    # 4. open assignment + driver rest
    assignment = _open_assignment_for_imei(imei)
    driver_payload = None
    if assignment:
        did = assignment.get("driver_id")
        drv = None
        try:
            drv = drivers_svc.get_driver(did) if did else None
        except Exception:
            drv = None
        rest_until = None
        if did:
            try:
                ru = drivers_svc.rest_until(did)
                rest_until = ru.isoformat() if ru else None
            except Exception:
                rest_until = None
        driver_payload = {
            "driver_id": did,
            "name":      drv.get("full_name") if drv else None,
            "rest_until": rest_until,
        }

    return jsonify({
        "imei":          imei,
        "truck_id":      name,
        "state":         st,
        "would_release": would_release,
        "release_groups": sorted(auto_release.RELEASE_GROUPS),
        "events":        events,
        "assignment":    assignment,
        "driver":        driver_payload,
    })