"""
app.py — Flask front-end for the Trip Review report.
"""
from __future__ import annotations

import json
import logging
import os
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

from flask import (
    Flask, render_template, request, redirect, url_for,
    send_from_directory, flash, jsonify, abort, session,
)

from config import TEMPLATE_PATH, OUTPUT_DIR, FLASK_SECRET, FLASK_DEBUG
from decorators import login_required
from jimi import RateLimitExceeded
from report import (
    compute_report, write_excel, write_bulk_excel,
    utc_str, find_trips,
)
from services.devices import classify_device, find_device, get_devices
from services import jobs
from services import reports_repo
from services import sessions as sessions_svc
from services import storage
from services.previews import (
    store_preview, get_preview, mark_ready_reports, mark_failed,
)
from services.ratelimit import status as ratelimit_status


# Defensive import of the audit module: a broken audit module must not
# prevent the app from booting.
try:
    from services import audit
    _AUDIT_OK = True
except Exception as _audit_exc:  # pragma: no cover
    logging.getLogger(__name__).error(
        "audit module unavailable — running with a no-op stub: %s", _audit_exc
    )
    _AUDIT_OK = False
    class _NoAudit:
        def record(self, *a, **k): return None
        def record_diff(self, *a, **k): return None
        def list_events(self, *a, **k):
            return {"rows": [], "page": 1, "per_page": 100, "has_more": False}
        def distinct_events(self, *a, **k): return []
        def distinct_severities(self, *a, **k): return []
        def list_alerts(self, *a, **k):
            return {"rows": [], "page": 1, "per_page": 50, "has_more": False}
        def unacked_alert_count(self): return 0
        def acknowledge_alert(self, *a, **k): return None
        def verify_chain(self, *a, **k):
            return {"ok": False, "error": "audit unavailable"}
        def prune(self, *a, **k): return {}
        def export_rows(self, *a, **k): return ("text/plain", "")
        def correlation_rows(self, *a, **k): return []
        def begin_request(self): return ""
        def current_request_id(self): return None
    audit = _NoAudit()


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger(__name__)


# Silence the supabase/httpx per-request INFO spam. Set LOG_HTTPX=1
# in the environment if you need to debug a specific supabase call.
if os.environ.get("LOG_HTTPX") != "1":
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

app = Flask(__name__)
app.secret_key = FLASK_SECRET

executor = ThreadPoolExecutor(max_workers=2)

MAX_TRUCKS          = 200
MAX_TRIPS_PER_TRUCK = 5


# --- blueprints -----------------------------------------------------------
from blueprints.auth       import bp as auth_bp
from blueprints.admin      import bp as admin_bp
from blueprints.live_fleet import bp as live_fleet_bp

app.register_blueprint(auth_bp)
app.register_blueprint(admin_bp)
app.register_blueprint(live_fleet_bp)


# --- audit middleware -----------------------------------------------------
try:
    from services.audit_middleware import register_audit_middleware
    register_audit_middleware(app)
except Exception as exc:
    log.error("audit middleware not registered (continuing without it): %s", exc)


# --- startup maintenance --------------------------------------------------
try:
    swept = jobs.sweep_stale()
    if swept:
        log.info("swept %d stale report job(s) on startup", swept)
except Exception as exc:
    log.warning("startup sweep failed: %s", exc)

try:
    purged = sessions_svc.purge_expired()
    if purged:
        log.info("purged %d expired session(s) on startup", purged)
except Exception as exc:
    log.warning("session purge failed: %s", exc)


# --- helpers --------------------------------------------------------------

def _to_jsonable(obj):
    if isinstance(obj, datetime):
        return {"$dt": obj.isoformat()}
    if isinstance(obj, dict):
        return {k: _to_jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_to_jsonable(v) for v in obj]
    return obj


def _from_jsonable(obj):
    if isinstance(obj, dict):
        if "$dt" in obj:
            return datetime.fromisoformat(obj["$dt"])
        return {k: _from_jsonable(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_from_jsonable(v) for v in obj]
    return obj


def _is_bulk_payload(stored) -> bool:
    return isinstance(stored, list)


@app.template_filter("fmt_duration")
def fmt_duration(seconds):
    try:
        total = int(seconds or 0)
    except (TypeError, ValueError):
        total = 0
    h = total // 3600
    m = (total % 3600) // 60
    s = total % 60
    return f"{h}:{m:02d}:{s:02d}"


def _collect_manual_from_form():
    signature = (request.form.get("meta_signature") or "").strip()
    raw = {
        "driver_name":   (request.form.get("meta_driver_name")   or "").strip(),
        "out_of_fuel":   (request.form.get("meta_out_of_fuel")   or "").strip(),
        "accidents":     (request.form.get("meta_accidents")     or "").strip(),
        "tyres_damaged": (request.form.get("meta_tyres")         or "").strip(),
        "breakdown":     (request.form.get("meta_breakdown")     or "").strip(),
    }
    out = {k: v for k, v in raw.items() if v}
    if signature:
        out["signature"] = signature
        out["signature_kind"] = "image"
    return out


def _parse_requested_trucks():
    raw = request.form.get("trucks") or "[]"
    try:
        items = json.loads(raw)
    except (ValueError, TypeError):
        return None
    if not isinstance(items, list):
        return None
    out = []
    seen = set()
    for it in items:
        if not isinstance(it, dict):
            continue
        imei = (it.get("imei") or "").strip()
        if not imei or imei in seen:
            continue
        seen.add(imei)
        out.append({
            "imei":   imei,
            "name":   (it.get("name") or imei).strip(),
            "driver": (it.get("driver") or "").strip(),
        })
    return out


def _group_trips_by_truck(selected: list[dict]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for s in selected:
        imei = (s.get("imei") or "").strip()
        if not imei:
            continue
        counts[imei] = counts.get(imei, 0) + 1
    return counts


def _unique_trucks_from_reports(reports: list[dict]) -> list[str]:
    out = []
    seen = set()
    for r in reports:
        tid = r.get("truck_id") or ""
        if tid and tid not in seen:
            seen.add(tid)
            out.append(tid)
    return out


def _bulk_truck_label(reports: list[dict]) -> str:
    trucks = _unique_trucks_from_reports(reports)
    if not trucks:
        return "Bulk"
    if len(trucks) == 1:
        return trucks[0]
    return f"Bulk ({len(trucks)} trucks)"


def _bulk_safe_filename_label(reports: list[dict]) -> str:
    trucks = _unique_trucks_from_reports(reports)
    if not trucks:
        return "Bulk"
    if len(trucks) == 1:
        label = trucks[0]
    else:
        label = f"Bulk {len(trucks)} trucks"
    return "".join(ch if ch.isalnum() or ch in " -" else "_"
                   for ch in label).strip()


def _merge_manual(old: dict | None, new: dict) -> dict:
    old = old or {}
    merged = dict(old)
    for k, v in new.items():
        if k in ("signature", "signature_kind") and not v:
            continue
        merged[k] = v
    return merged


def _normalise_submitted_manual(raw: dict) -> dict:
    if not isinstance(raw, dict):
        return {}

    out: dict = {}
    mapping = {
        "meta_driver_name": "driver_name",
        "meta_out_of_fuel": "out_of_fuel",
        "meta_accidents":   "accidents",
        "meta_tyres":       "tyres_damaged",
        "meta_breakdown":   "breakdown",
    }
    for form_key, manual_key in mapping.items():
        val = (raw.get(form_key) or "").strip()
        if val:
            out[manual_key] = val

    sig = (raw.get("meta_signature") or "").strip()
    if sig:
        out["signature"] = sig
        out["signature_kind"] = raw.get("meta_signature_kind") or "image"

    return out


# --- background workers ---------------------------------------------------

def _run_preview(preview_id, trips, include_history):
    try:
        reports = []
        for trip in trips:
            t_start = datetime.strptime(
                trip["start"], "%Y-%m-%d %H:%M:%S"
            ).replace(tzinfo=timezone.utc)
            t_end = datetime.strptime(
                trip["end"], "%Y-%m-%d %H:%M:%S"
            ).replace(tzinfo=timezone.utc)
            r = compute_report(
                imei=trip["imei"],
                truck_id=trip["truck"],
                driver_name=trip.get("driver") or "",
                begin=t_start, end=t_end,
                include_history=include_history,
            )
            reports.append(r)
        mark_ready_reports(preview_id, reports)
    except RateLimitExceeded as exc:
        log.warning("preview hit rate limit: %s", exc)
        mark_failed(preview_id,
                    "Tracksolid Pro daily API limit reached. "
                    "Your report could not be generated.")
    except Exception as exc:
        log.exception("preview failed")
        mark_failed(preview_id, f"{type(exc).__name__}: {exc}")


def _run_export(job_id, computed, out_path, manual):
    jobs.mark_running(job_id)
    try:
        write_excel(computed, TEMPLATE_PATH, out_path, manual=manual)
        size = out_path.stat().st_size if out_path.exists() else None
    except Exception as exc:
        log.exception("export failed")
        jobs.mark_failed(job_id, error=f"{type(exc).__name__}: {exc}")
        return

    remote = None
    try:
        remote = storage.upload(out_path)
    except Exception:
        log.exception("storage upload raised for %s", out_path.name)

    if remote:
        log.info("report mirrored to storage: %s", remote)
    else:
        log.error(
            "report %s NOT mirrored to storage — it will be undownloadable "
            "after the next container restart. Check the Supabase 'reports' "
            "bucket exists and accepts uploads.",
            out_path.name,
        )

    jobs.mark_done(job_id, filesize_bytes=size)

    try:
        reports_repo.save_computed(job_id, _to_jsonable(computed))
        reports_repo.update_manual(job_id, manual)
    except Exception:
        log.exception("could not save computed/manual for %s", job_id)


def _run_bulk_export(job_id, reports, out_path, per_trip_manual):
    jobs.mark_running(job_id)
    try:
        write_bulk_excel(reports, TEMPLATE_PATH, out_path,
                         per_trip_manual=per_trip_manual)
        size = out_path.stat().st_size if out_path.exists() else None
    except Exception as exc:
        log.exception("bulk export failed")
        jobs.mark_failed(job_id, error=f"{type(exc).__name__}: {exc}")
        return

    remote = None
    try:
        remote = storage.upload(out_path)
    except Exception:
        log.exception("bulk storage upload raised for %s", out_path.name)

    if remote:
        log.info("bulk report mirrored to storage: %s", remote)
    else:
        log.error(
            "bulk report %s NOT mirrored to storage — it will be "
            "undownloadable after the next container restart. Check the "
            "Supabase 'reports' bucket exists and accepts uploads.",
            out_path.name,
        )

    jobs.mark_done(job_id, filesize_bytes=size)

    try:
        reports_repo.save_computed(job_id, _to_jsonable(list(reports)))
        reports_repo.update_manual(job_id, per_trip_manual or {})
    except Exception:
        log.exception("could not save computed/manual for bulk %s", job_id)


# --- nav context ----------------------------------------------------------

_nav_alert_cache: dict = {"value": 0, "expires_at": 0.0}


@app.context_processor
def _inject_nav_context():
    ctx = {"nav_unacked_alerts": 0, "nav_pending_jobs": 0}
    if not session.get("user_id"):
        return ctx
    try:
        if session.get("role") == "super_admin":
            now = time.time()
            if now >= _nav_alert_cache["expires_at"]:
                _nav_alert_cache["value"] = audit.unacked_alert_count()
                _nav_alert_cache["expires_at"] = now + 10.0
            ctx["nav_unacked_alerts"] = _nav_alert_cache["value"]
        ctx["nav_pending_jobs"] = jobs.count_pending(session.get("user_id"))
    except Exception:
        pass
    return ctx


# --- routes ---------------------------------------------------------------

@app.route("/favicon.ico")
def favicon():
    return send_from_directory(
        app.static_folder, "gbc_logo.png", mimetype="image/png",
    )


@app.route("/healthz")
def healthz():
    return jsonify({
        "status": "ok",
        "time":   datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
    })


@app.errorhandler(403)
def forbidden(e):
    return render_template("403.html"), 403


@app.route("/")
@login_required
def index():
    return render_template("index.html")


@app.route("/api/status")
def api_status():
    return jsonify(ratelimit_status())


@app.route("/api/devices")
@login_required
def api_devices():
    include_operational = request.args.get("include_operational") == "1"
    try:
        devices = get_devices()
    except Exception as exc:
        log.exception("device list fetch failed")
        return jsonify({"error": f"{type(exc).__name__}: {exc}"}), 502

    out = []
    for d in devices:
        imei = d.get("imei")
        if not imei:
            continue
        name = d.get("deviceName") or imei
        category = classify_device(name)
        if category != "regular" and not include_operational:
            continue
        out.append({
            "imei":     imei,
            "name":     name,
            "driver":   d.get("driverName") or "",
            "category": category,
        })
    out.sort(key=lambda x: x["name"].lower())
    return jsonify({"devices": out})
@app.route("/trips", methods=["POST"])
@login_required
def trips_list():
    trucks          = _parse_requested_trucks()
    begin_s         = (request.form.get("begin") or "").strip()
    end_s           = (request.form.get("end") or "").strip()
    include_history = request.form.get("include_history") == "on"

    if not trucks:
        flash("Please select at least one truck.", "error")
        return redirect(url_for("index"))

    if len(trucks) > MAX_TRUCKS:
        flash(f"Please select at most {MAX_TRUCKS} trucks per search.", "error")
        return redirect(url_for("index"))

    if not begin_s or not end_s:
        flash("Begin and end dates are required.", "error")
        return redirect(url_for("index"))

    try:
        begin = datetime.strptime(begin_s, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
        end   = datetime.strptime(end_s,   "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    except ValueError:
        flash("Dates must be 'YYYY-MM-DD HH:MM:SS'.", "error")
        return redirect(url_for("index"))

    audit.record(
        "search.trips",
        target=", ".join(t.get("name") or t["imei"] for t in trucks)[:200],
        detail=(f"trucks={len(trucks)}, begin={begin_s}, end={end_s}, "
                f"include_history={include_history}"),
        meta={"truck_count": len(trucks), "include_history": include_history,
              "begin": begin_s, "end": end_s},
    )

    resolved = []
    not_found = []
    for t in trucks:
        device = find_device(t["imei"])
        if not device:
            not_found.append(t["name"])
            continue
        resolved.append({
            "imei":        device["imei"],
            "truck":       device.get("deviceName") or device["imei"],
            "driver_name": device.get("driverName") or "",
        })

    if not resolved:
        flash("None of the selected trucks could be found.", "error")
        return redirect(url_for("index"))

    groups = []
    total  = 0
    rate_limited = False
    for dev in resolved:
        try:
            trips = find_trips(dev["imei"], begin, end)
        except RateLimitExceeded as exc:
            log.warning("trip list hit rate limit: %s", exc)
            rate_limited = True
            break
        except Exception as exc:
            log.exception("trip list failed for %s", dev["imei"])
            flash(f"Could not load trips for {dev['truck']}: {exc}", "error")
            continue
        if not trips:
            continue
        groups.append({
            "imei":        dev["imei"],
            "truck":       dev["truck"],
            "driver_name": dev["driver_name"],
            "trips":       trips,
        })
        total += len(trips)

    if rate_limited:
        flash("Tracksolid Pro daily API limit reached. Try again later.", "error")
        return redirect(url_for("index"))

    if not groups:
        flash("No trips found for any of the selected trucks in that window.", "error")
        return redirect(url_for("index"))

    return render_template(
        "trips.html",
        truck_groups=groups, total_trips=total, not_found=not_found,
        begin_str=begin_s, end_str=end_s, include_history=include_history,
        max_per_truck=MAX_TRIPS_PER_TRUCK,
    )


@app.route("/preview", methods=["POST"])
@login_required
def preview_start():
    selected_raw = request.form.get("selected_trips") or "[]"
    include_history = request.form.get("include_history") == "on"

    try:
        selected = json.loads(selected_raw)
    except (ValueError, TypeError):
        selected = []

    if not isinstance(selected, list) or not selected:
        flash("Select at least one trip.", "error")
        return redirect(url_for("index"))

    per_truck = _group_trips_by_truck(selected)
    for imei, n in per_truck.items():
        if n > MAX_TRIPS_PER_TRUCK:
            flash(f"At most {MAX_TRIPS_PER_TRUCK} trips per truck. "
                  f"One truck has {n}.", "error")
            return redirect(url_for("index"))

    for s in selected:
        for key in ("imei", "truck", "start", "end"):
            if not (s.get(key) or "").strip():
                flash("Invalid trip selection.", "error")
                return redirect(url_for("index"))

    preview_id = uuid.uuid4().hex[:12]
    store_preview(preview_id, {
        "status":          "computing",
        "queued_at":       datetime.now(timezone.utc),
        "truck":           ", ".join(sorted({s["truck"] for s in selected}))[:120],
        "imei":            selected[0]["imei"],
        "driver_name":     "",
        "trips":           selected,
        "reports":         None,
        "error":           None,
        "created_by":      session.get("user_id"),
        "include_history": include_history,
    })
    executor.submit(_run_preview, preview_id, selected, include_history)
    return redirect(url_for("preview_show", preview_id=preview_id))


@app.route("/preview/<preview_id>")
@login_required
def preview_show(preview_id):
    state = get_preview(preview_id)
    if not state:
        flash("Preview expired or not found.", "error")
        return redirect(url_for("index"))
    if (state.get("created_by")
            and state["created_by"] != session.get("user_id")
            and session.get("role") not in ("admin", "super_admin")):
        abort(403)

    if state["status"] == "computing":
        return render_template("preview.html", loading=True,
                               preview_id=preview_id, state=state)

    if state["status"] == "failed":
        flash(state.get("error") or "Preview failed.", "error")
        return redirect(url_for("index"))

    reports = state.get("reports") or []
    if not reports:
        flash("Preview has no data.", "error")
        return redirect(url_for("index"))

    try:
        current_index = int(request.args.get("trip", 0))
    except (ValueError, TypeError):
        current_index = 0
    current_index = max(0, min(current_index, len(reports) - 1))

    return render_template(
        "preview.html", loading=False, view_mode=False, is_bulk=False,
        preview_id=preview_id, state=state, reports=reports,
        current_index=current_index, computed=reports[current_index],
        manual={}, report_id=None,
    )


@app.route("/api/preview/<preview_id>")
@login_required
def api_preview(preview_id):
    state = get_preview(preview_id)
    if not state:
        abort(404)
    if (state.get("created_by")
            and state["created_by"] != session.get("user_id")
            and session.get("role") not in ("admin", "super_admin")):
        abort(403)
    return jsonify({"status": state["status"], "error": state.get("error")})


@app.route("/export", methods=["POST"])
@login_required
def export():
    preview_id = request.form.get("preview_id", "").strip()
    try:
        trip_index = int(request.form.get("trip_index", "0"))
    except (ValueError, TypeError):
        trip_index = 0

    state = get_preview(preview_id)
    if not state or state["status"] != "ready":
        flash("Preview not ready or expired. Please search again.", "error")
        return redirect(url_for("index"))
    if (state.get("created_by")
            and state["created_by"] != session.get("user_id")
            and session.get("role") not in ("admin", "super_admin")):
        abort(403)

    reports = state.get("reports") or []
    if trip_index < 0 or trip_index >= len(reports):
        flash("Invalid trip selection.", "error")
        return redirect(url_for("preview_show", preview_id=preview_id))
    computed = reports[trip_index]

    manual = _collect_manual_from_form()

    safe_name = "".join(ch if ch.isalnum() or ch in " -" else "_"
                        for ch in computed["truck_id"]).strip()
    run_stamp = datetime.now(timezone.utc).strftime("%H%M%S")
    date_str  = (computed["trip_start"].date()
                 if computed["trip_start"] else datetime.now(timezone.utc).date())
    filename  = f"Trip Review - {safe_name} - {date_str} - {run_stamp}.xlsx"
    out_path  = OUTPUT_DIR / filename

    try:
        job_id = jobs.add_job(
            truck_id=computed["truck_id"], imei=computed["imei"],
            filename=filename, trip_start=computed["trip_start"],
            trip_end=computed["trip_end"],
            created_by=session.get("user_id"),
            created_by_email=session.get("email"), manual=manual,
        )
    except Exception as exc:
        log.exception("could not create report row")
        flash(f"Could not queue export: {exc}", "error")
        return redirect(url_for("preview_show", preview_id=preview_id,
                                trip=trip_index))

    executor.submit(_run_export, job_id, computed, out_path, manual)
    audit.record(
        "report.export",
        target=computed["truck_id"],
        detail=(f"filename={filename}, trip_index={trip_index}, "
                f"trip_start={utc_str(computed['trip_start']) if computed['trip_start'] else ''}"),
        meta={"job_id": job_id, "filename": filename,
              "trip_index": trip_index, "imei": computed["imei"],
              "manual_keys": list(manual.keys())},
    )
    flash(f"Export queued for {computed['truck_id']} "
          f"(trip {trip_index + 1} of {len(reports)}). "
          f"Download from the Reports page.", "success")
    return redirect(url_for("reports"))


@app.route("/export-bulk", methods=["POST"])
@login_required
def export_bulk():
    preview_id = request.form.get("preview_id", "").strip()
    scope      = (request.form.get("scope") or "all").strip()

    state = get_preview(preview_id)
    if not state or state["status"] != "ready":
        flash("Preview not ready or expired. Please search again.", "error")
        return redirect(url_for("index"))
    if (state.get("created_by")
            and state["created_by"] != session.get("user_id")
            and session.get("role") not in ("admin", "super_admin")):
        abort(403)

    all_reports = state.get("reports") or []
    if not all_reports:
        flash("Preview has no data.", "error")
        return redirect(url_for("preview_show", preview_id=preview_id))

    submitted_manual: dict[str, dict] = {}
    per_trip_manual_raw = request.form.get("per_trip_manual") or "{}"
    try:
        parsed = json.loads(per_trip_manual_raw)
        if isinstance(parsed, dict):
            for k, v in parsed.items():
                if isinstance(v, dict):
                    submitted_manual[str(k)] = _normalise_submitted_manual(v)
    except (ValueError, TypeError):
        log.warning("export_bulk: could not parse per_trip_manual")

    if scope == "truck":
        truck_filter = (request.form.get("truck") or "").strip()
        if not truck_filter:
            flash("No truck specified for bulk export.", "error")
            return redirect(url_for("preview_show", preview_id=preview_id))
        all_indices = [i for i, r in enumerate(all_reports)
                       if r["truck_id"] == truck_filter]
        if not all_indices:
            flash("No reports for that truck in this preview.", "error")
            return redirect(url_for("preview_show", preview_id=preview_id))
        reports = [all_reports[i] for i in all_indices]
        per_trip_manual: dict[str, dict] = {}
        for new_idx, orig_idx in enumerate(all_indices):
            if str(orig_idx) in submitted_manual:
                per_trip_manual[str(new_idx)] = submitted_manual[str(orig_idx)]
    else:
        reports = list(all_reports)
        per_trip_manual = submitted_manual

    bulk_label = _bulk_truck_label(reports)
    safe_label = _bulk_safe_filename_label(reports)
    run_stamp  = datetime.now(timezone.utc).strftime("%H%M%S")
    today      = datetime.now(timezone.utc).date()
    filename   = f"Trip Review - {safe_label} - {today} - {run_stamp}.xlsx"
    out_path   = OUTPUT_DIR / filename

    starts = [r["trip_start"] for r in reports if r.get("trip_start")]
    ends   = [r["trip_end"]   for r in reports if r.get("trip_end")]
    job_trip_start = min(starts) if starts else None
    job_trip_end   = max(ends)   if ends   else None

    try:
        job_id = jobs.add_job(
            truck_id=bulk_label, imei=None, filename=filename,
            trip_start=job_trip_start, trip_end=job_trip_end,
            created_by=session.get("user_id"),
            created_by_email=session.get("email"), manual=None,
        )
    except Exception as exc:
        log.exception("could not create bulk report row")
        flash(f"Could not queue bulk export: {exc}", "error")
        return redirect(url_for("preview_show", preview_id=preview_id))

    executor.submit(_run_bulk_export, job_id, reports, out_path, per_trip_manual)
    truck_count = len({r["truck_id"] for r in reports})
    audit.record(
        "report.export_bulk",
        target=bulk_label,
        detail=(f"filename={filename}, scope={scope}, "
                f"trips={len(reports)}, trucks={truck_count}"),
        meta={"job_id": job_id, "filename": filename, "scope": scope,
              "trip_count": len(reports), "truck_count": truck_count},
    )
    flash(f"Bulk export queued: {len(reports)} trip(s) across "
          f"{truck_count} truck(s). Download from the Reports page.", "success")
    return redirect(url_for("reports"))


@app.route("/reports")
@login_required
def reports():
    is_admin = session.get("role") in ("admin", "super_admin")
    jobs_list = jobs.all_jobs_sorted_for(
        session.get("user_id"), show_all=is_admin
    )
    return render_template("reports.html", jobs=jobs_list, is_admin=is_admin)


@app.route("/reports/<report_id>/view")
@login_required
def report_view(report_id):
    row = reports_repo.get_full(report_id)
    if not row:
        flash("Report not found.", "error")
        return redirect(url_for("reports"))

    owner = row.get("created_by")
    if (owner
            and owner != session.get("user_id")
            and session.get("role") not in ("admin", "super_admin")):
        abort(403)

    stored = row.get("computed_json")
    if not stored:
        flash("This report has no stored detail — regenerate it to view.",
              "error")
        return redirect(url_for("reports"))

    stored = _from_jsonable(stored)
    is_bulk = _is_bulk_payload(stored)
    reports = stored if is_bulk else [stored]
    if not reports:
        flash("This report is empty.", "error")
        return redirect(url_for("reports"))

    try:
        current_index = int(request.args.get("trip", 0))
    except (ValueError, TypeError):
        current_index = 0
    current_index = max(0, min(current_index, len(reports) - 1))

    existing_manual = row.get("manual_json") or {}
    if is_bulk:
        current_manual = existing_manual.get(str(current_index), {})
    else:
        current_manual = existing_manual

    if current_index == 0:
        audit.record(
            "report.view",
            target=row.get("truck_id") or report_id,
            detail=f"filename={row.get('filename')}, bulk={is_bulk}, "
                   f"trips={len(reports)}",
            meta={"report_id": report_id, "is_bulk": is_bulk},
        )

    return render_template(
        "preview.html", loading=False, view_mode=True, is_bulk=is_bulk,
        preview_id=report_id, report_id=report_id,
        state={"truck": row.get("truck_id"), "imei": row.get("imei")},
        reports=reports, current_index=current_index,
        computed=reports[current_index], manual=current_manual,
    )


@app.route("/reports/<report_id>/edit-meta", methods=["POST"])
@login_required
def report_edit_meta(report_id):
    row = reports_repo.get_full(report_id)
    if not row:
        flash("Report not found.", "error")
        return redirect(url_for("reports"))

    owner = row.get("created_by")
    if (owner
            and owner != session.get("user_id")
            and session.get("role") not in ("admin", "super_admin")):
        abort(403)

    stored = row.get("computed_json")
    if not stored:
        flash("This report has no stored detail — regenerate it first.",
              "error")
        return redirect(url_for("reports"))

    stored = _from_jsonable(stored)
    is_bulk = _is_bulk_payload(stored)
    reports = stored if is_bulk else [stored]
    if not reports:
        flash("This report is empty.", "error")
        return redirect(url_for("reports"))

    try:
        trip_index = int(request.form.get("trip_index", "0"))
    except (ValueError, TypeError):
        trip_index = 0
    if trip_index < 0 or trip_index >= len(reports):
        flash("Invalid trip selection.", "error")
        return redirect(url_for("report_view", report_id=report_id))
    computed = reports[trip_index]

    incoming = _collect_manual_from_form()

    filename = row.get("filename")
    if not filename:
        flash("Report has no filename — cannot regenerate.", "error")
        return redirect(url_for("reports"))

    out_path = OUTPUT_DIR / filename
    before_manual = {}
    after_manual  = {}

    try:
        if is_bulk:
            existing = row.get("manual_json") or {}
            if not isinstance(existing, dict):
                existing = {}
            per_trip_manual: dict[str, dict] = {}
            for i, _r in enumerate(reports):
                key = str(i)
                if i == trip_index:
                    per_trip_manual[key] = _merge_manual(
                        existing.get(key) or {}, incoming)
                elif key in existing and isinstance(existing[key], dict):
                    per_trip_manual[key] = existing[key]
                else:
                    per_trip_manual[key] = {}
            before_manual = (existing.get(str(trip_index)) or {})
            after_manual  = per_trip_manual.get(str(trip_index), {})
            write_bulk_excel(reports, TEMPLATE_PATH, out_path,
                             per_trip_manual=per_trip_manual)
            size = out_path.stat().st_size if out_path.exists() else None
            reports_repo.update_manual(report_id, per_trip_manual,
                                       filesize_bytes=size)
        else:
            merged_manual = _merge_manual(row.get("manual_json") or {}, incoming)
            before_manual = row.get("manual_json") or {}
            after_manual  = merged_manual
            write_excel(computed, TEMPLATE_PATH, out_path, manual=merged_manual)
            size = out_path.stat().st_size if out_path.exists() else None
            reports_repo.update_manual(report_id, merged_manual,
                                       filesize_bytes=size)

        try:
            remote = storage.upload(out_path)
            if remote:
                log.info("regenerated report mirrored to storage: %s", remote)
            else:
                log.error(
                    "regenerated report %s NOT mirrored to storage — it will "
                    "be undownloadable after the next container restart.",
                    out_path.name,
                )
        except Exception:
            log.exception("storage mirror failed for %s", out_path.name)

        audit.record_diff(
            "report.edit_meta",
            target=row.get("truck_id") or report_id,
            before={"manual": before_manual},
            after={"manual": after_manual},
            detail=(f"filename={filename}, trip_index={trip_index}, "
                    f"bulk={is_bulk}"),
            meta={"report_id": report_id, "trip_index": trip_index,
                  "is_bulk": is_bulk},
        )

        if is_bulk:
            flash(f"Trip {trip_index + 1} ({computed['truck_id']}) updated; "
                  f"bulk workbook regenerated.", "success")
            return redirect(url_for("report_view", report_id=report_id,
                                    trip=trip_index))
        flash("Report details updated and Excel regenerated.", "success")
    except Exception as exc:
        log.exception("edit-meta failed")
        flash(f"Could not update report: {exc}", "error")
        return redirect(url_for("report_view", report_id=report_id))

    return redirect(url_for("reports"))


@app.route("/api/jobs")
@login_required
def api_jobs():
    is_admin = session.get("role") in ("admin", "super_admin")
    scope = request.args.get("scope", "all" if is_admin else "mine")
    show_all = is_admin and scope == "all"
    jobs_list = jobs.all_jobs_sorted_for(session.get("user_id"), show_all=show_all)
    return jsonify([{
        "id":               j["id"],
        "truck":            j["truck"],
        "status":           j["status"],
        "filename":         j["filename"],
        "queued_at":        utc_str(j["queued_at"]) if j["queued_at"] else None,
        "finished_at":      utc_str(j["finished_at"]) if j["finished_at"] else None,
        "trip_start":       j.get("trip_start"),
        "trip_end":         j.get("trip_end"),
        "error":            j["error"],
        "from_disk":        j["from_disk"],
        "created_by_email": j.get("created_by_email"),
    } for j in jobs_list])


@app.route("/download/<path:filename>")
@login_required
def download(filename):
    owner = jobs.get_report_owner(filename)
    if (owner
            and owner != session.get("user_id")
            and session.get("role") not in ("admin", "super_admin")):
        abort(403)

    local_path = OUTPUT_DIR / filename

    if local_path.exists():
        log.info("download: serving %s from local disk", filename)
        audit.record(
            "report.download", target=filename,
            detail=f"source=local, size={local_path.stat().st_size} bytes",
            meta={"filename": filename, "source": "local",
                  "size": local_path.stat().st_size},
            tags=["download"],
        )
        return send_from_directory(OUTPUT_DIR, filename, as_attachment=True)

    log.info("download: %s missing on disk; trying Supabase Storage", filename)

    try:
        ok = storage.download_to(local_path, filename)
    except Exception:
        log.exception("download: storage.download_to raised for %s", filename)
        ok = False

    if ok:
        log.info("download: serving %s from storage fallback", filename)
        size = local_path.stat().st_size if local_path.exists() else None
        audit.record(
            "report.download", target=filename,
            detail=f"source=storage, size={size} bytes",
            meta={"filename": filename, "source": "storage", "size": size},
            tags=["download"],
        )
        return send_from_directory(OUTPUT_DIR, filename, as_attachment=True)

    log.warning(
        "download: 404 for %s — not on disk and not retrievable from the "
        "Supabase 'reports' bucket. Verify the bucket exists and that the "
        "original upload succeeded (search the logs for 'NOT mirrored').",
        filename,
    )
    audit.record(
        "report.download_failed", target=filename,
        detail="not on disk and not in storage", severity="warning",
        outcome="failure",
    )
    abort(404)


if __name__ == "__main__":
    app.run(debug=FLASK_DEBUG, host="0.0.0.0", port=5000)