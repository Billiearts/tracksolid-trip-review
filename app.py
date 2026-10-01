"""
app.py — Flask front-end for the Trip Review report.
"""
from __future__ import annotations
import os
import logging
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
from report import compute_report, write_excel, utc_str
from services.devices import find_device, get_devices
from services import jobs
from services import reports_repo
from services import sessions as sessions_svc
from services import storage
from services.previews import store_preview, get_preview, mark_ready, mark_failed
from services.ratelimit import status as ratelimit_status


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger(__name__)

OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

app = Flask(__name__)
app.secret_key = FLASK_SECRET

executor = ThreadPoolExecutor(max_workers=2)


# --- blueprints -----------------------------------------------------------
from blueprints.auth  import bp as auth_bp
from blueprints.admin import bp as admin_bp

app.register_blueprint(auth_bp)
app.register_blueprint(admin_bp)


# --- JSON (de)serialisation for stored reports ----------------------------

def _to_jsonable(obj):
    """Recursively convert datetimes to a marked dict for JSON storage."""
    if isinstance(obj, datetime):
        return {"$dt": obj.isoformat()}
    if isinstance(obj, dict):
        return {k: _to_jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_to_jsonable(v) for v in obj]
    return obj


def _from_jsonable(obj):
    """Reverse of _to_jsonable."""
    if isinstance(obj, dict):
        if "$dt" in obj:
            return datetime.fromisoformat(obj["$dt"])
        return {k: _from_jsonable(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_from_jsonable(v) for v in obj]
    return obj


@app.template_filter("fmt_duration")
def fmt_duration(seconds):
    """Render seconds as H:MM:SS."""
    try:
        total = int(seconds or 0)
    except (TypeError, ValueError):
        total = 0
    h = total // 3600
    m = (total % 3600) // 60
    s = total % 60
    return f"{h}:{m:02d}:{s:02d}"


# --- background workers ---------------------------------------------------

def _run_preview(preview_id: str, imei: str, truck_id: str, driver_name: str,
                 begin: datetime, end: datetime, include_history: bool) -> None:
    try:
        computed = compute_report(
            imei=imei, truck_id=truck_id, driver_name=driver_name,
            begin=begin, end=end, include_history=include_history,
        )
        mark_ready(preview_id, computed)
    except RateLimitExceeded as exc:
        log.warning("preview hit rate limit: %s", exc)
        mark_failed(preview_id,
                    "Tracksolid Pro daily API limit reached. "
                    "Your report could not be generated.")
    except Exception as exc:
        log.exception("preview failed")
        mark_failed(preview_id, f"{type(exc).__name__}: {exc}")


def _run_export(job_id: str, computed: dict, out_path) -> None:
    jobs.mark_running(job_id)
    try:
        write_excel(computed, TEMPLATE_PATH, out_path)
        size = out_path.stat().st_size if out_path.exists() else None
        jobs.mark_done(job_id, filesize_bytes=size)
    except Exception as exc:
        log.exception("export failed")
        jobs.mark_failed(job_id, error=f"{type(exc).__name__}: {exc}")
        return

    # Save the full computed report as JSON for later viewing.
    try:
        reports_repo.save_computed(job_id, _to_jsonable(computed))
    except Exception:
        log.exception("could not save computed JSON for %s", job_id)

    # Mirror to Supabase Storage — best effort.
    try:
        remote = storage.upload(out_path)
        if remote:
            log.info("report mirrored to storage: %s", remote)
        else:
            log.warning("report not mirrored: %s", out_path.name)
    except Exception:
        log.exception("storage mirror failed for %s", out_path.name)


# --- routes ---------------------------------------------------------------

@app.route("/favicon.ico")
def favicon():
    return send_from_directory(
        app.static_folder, "gbc_logo.png",
        mimetype="image/png",
    )


@app.route("/healthz")
def healthz():
    return jsonify({
        "status": "ok",
        "time":   datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
    })

@app.route("/debug/env")
def debug_env():
    """TEMPORARY — remove after diagnosing the Tracksolid auth issue."""
    import hashlib as _hl
    out = {}
    for name in ("TRACKSOLID_APP_KEY",
                 "TRACKSOLID_APP_SECRET",
                 "TRACKSOLID_USER_ID",
                 "TRACKSOLID_USER_PWD_MD5"):
        val = os.environ.get(name, "")
        out[name] = {
            "len":    len(val),
            "sha256": _hl.sha256(val.encode()).hexdigest()[:16],
        }
    return jsonify(out)

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
        out.append({
            "imei":   imei,
            "name":   d.get("deviceName") or imei,
            "driver": d.get("driverName") or "",
        })
    out.sort(key=lambda x: x["name"].lower())
    return jsonify({"devices": out})


@app.route("/preview", methods=["POST"])
@login_required
def preview_start():
    truck   = (request.form.get("truck") or "").strip()
    begin_s = (request.form.get("begin") or "").strip()
    end_s   = (request.form.get("end") or "").strip()
    include_history = request.form.get("include_history") == "on"

    if not truck or not begin_s or not end_s:
        flash("All three fields are required.", "error")
        return redirect(url_for("index"))
    try:
        begin = datetime.strptime(begin_s, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
        end   = datetime.strptime(end_s,   "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    except ValueError:
        flash("Dates must be 'YYYY-MM-DD HH:MM:SS'.", "error")
        return redirect(url_for("index"))

    device = find_device(truck)
    if not device:
        flash(f"No unique device matched '{truck}'.", "error")
        return redirect(url_for("index"))

    imei        = device["imei"]
    device_name = device.get("deviceName") or imei
    driver_name = device.get("driverName") or ""

    preview_id = uuid.uuid4().hex[:12]
    store_preview(preview_id, {
        "status":      "computing",
        "queued_at":   datetime.now(timezone.utc),
        "truck":       device_name,
        "imei":        imei,
        "driver_name": driver_name,
        "begin":       begin,
        "end":         end,
        "data":        None,
        "error":       None,
        "created_by":  session.get("user_id"),
    })
    executor.submit(_run_preview, preview_id, imei, device_name,
                    driver_name, begin, end, include_history)
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
            and session.get("role") != "admin"):
        abort(403)
    if state["status"] == "computing":
        return render_template("preview.html",
                               loading=True, preview_id=preview_id,
                               state=state)
    if state["status"] == "failed":
        flash(state.get("error") or "Preview failed.", "error")
        return redirect(url_for("index"))
    return render_template("preview.html",
                           loading=False, preview_id=preview_id,
                           state=state, computed=state["data"])


@app.route("/api/preview/<preview_id>")
@login_required
def api_preview(preview_id):
    state = get_preview(preview_id)
    if not state:
        abort(404)
    if (state.get("created_by")
            and state["created_by"] != session.get("user_id")
            and session.get("role") != "admin"):
        abort(403)
    return jsonify({"status": state["status"], "error": state.get("error")})


@app.route("/export", methods=["POST"])
@login_required
def export():
    preview_id = request.form.get("preview_id", "").strip()
    state = get_preview(preview_id)
    if not state or state["status"] != "ready":
        flash("Preview not ready or expired. Please search again.", "error")
        return redirect(url_for("index"))
    if (state.get("created_by")
            and state["created_by"] != session.get("user_id")
            and session.get("role") != "admin"):
        abort(403)

    computed = state["data"]
    safe_name = "".join(ch if ch.isalnum() or ch in " -" else "_"
                        for ch in computed["truck_id"]).strip()
    run_stamp = datetime.now(timezone.utc).strftime("%H%M%S")
    date_str  = (computed["trip_start"].date()
                 if computed["trip_start"] else datetime.now(timezone.utc).date())
    filename  = f"Trip Review - {safe_name} - {date_str} - {run_stamp}.xlsx"
    out_path  = OUTPUT_DIR / filename

    try:
        job_id = jobs.add_job(
            truck_id=computed["truck_id"],
            imei=computed["imei"],
            filename=filename,
            trip_start=computed["trip_start"],
            trip_end=computed["trip_end"],
            created_by=session.get("user_id"),
            created_by_email=session.get("email"),
        )
    except Exception as exc:
        log.exception("could not create report row")
        flash(f"Could not queue export: {exc}", "error")
        return redirect(url_for("reports"))

    executor.submit(_run_export, job_id, computed, out_path)
    flash(f"Export queued for {computed['truck_id']}.", "success")
    return redirect(url_for("reports"))


@app.route("/reports")
@login_required
def reports():
    is_admin = session.get("role") == "admin"
    jobs_list = jobs.all_jobs_sorted_for(
        session.get("user_id"), show_all=is_admin
    )
    return render_template("reports.html",
                           jobs=jobs_list,
                           is_admin=is_admin)


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
            and session.get("role") != "admin"):
        abort(403)

    computed = row.get("computed_json")
    if not computed:
        flash("This report has no stored detail — regenerate it to view.",
              "error")
        return redirect(url_for("reports"))

    computed = _from_jsonable(computed)

    return render_template(
        "preview.html",
        loading=False,
        view_mode=True,
        preview_id=report_id,
        state={
            "truck": row.get("truck_id"),
            "imei":  row.get("imei"),
        },
        computed=computed,
    )


@app.route("/api/jobs")
@login_required
def api_jobs():
    is_admin = session.get("role") == "admin"
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
            and session.get("role") != "admin"):
        abort(403)

    local_path = OUTPUT_DIR / filename

    if local_path.exists():
        return send_from_directory(OUTPUT_DIR, filename, as_attachment=True)

    if storage.download_to(local_path, filename):
        return send_from_directory(OUTPUT_DIR, filename, as_attachment=True)

    abort(404)


if __name__ == "__main__":
    app.run(debug=FLASK_DEBUG, host="127.0.0.1", port=5000)