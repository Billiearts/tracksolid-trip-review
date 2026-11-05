"""
app.py — Flask front-end for the Trip Review report.
"""
from __future__ import annotations

import json
import logging
import os
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
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
from services.ratelimit import status as ratelimit_status, is_rate_limited
from services import report_jobs
from services import job_worker
from report_analysis import YARD_LOOKBACK, history_window
from services.supabase_client import admin_client

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


if os.environ.get("LOG_HTTPX") != "1":
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

app = Flask(__name__)
app.secret_key = FLASK_SECRET


def _int_env(name: str, default: int, lo: int, hi: int) -> int:
    try:
        v = int(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        v = default
    return max(lo, min(hi, v))


_TRIPS_WORKERS   = _int_env("TRIPS_FETCH_WORKERS",     4, 1, 16)
_PREVIEW_WORKERS = _int_env("PREVIEW_COMPUTE_WORKERS", 3, 1, 8)

_trips_pool   = ThreadPoolExecutor(max_workers=_TRIPS_WORKERS,
                                   thread_name_prefix="trips-fetch")
_preview_pool = ThreadPoolExecutor(max_workers=_PREVIEW_WORKERS,
                                   thread_name_prefix="preview-compute")

executor = ThreadPoolExecutor(max_workers=2)

MAX_TRUCKS          = 200
MAX_TRIPS_PER_TRUCK = 5


from blueprints.auth       import bp as auth_bp
from blueprints.admin      import bp as admin_bp
from blueprints.live_fleet import bp as live_fleet_bp

app.register_blueprint(auth_bp)
app.register_blueprint(admin_bp)
app.register_blueprint(live_fleet_bp)


try:
    from services.audit_middleware import register_audit_middleware
    register_audit_middleware(app)
except Exception as exc:
    log.error("audit middleware not registered (continuing without it): %s", exc)


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
        if not isinstance(r, dict):
            continue
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


def _preview_job_id_from_form() -> str | None:
    raw = (request.form.get("preview_job_id") or "").strip()
    return raw or None


def _resolve_export_context():
    preview_id       = (request.form.get("preview_id") or "").strip()
    preview_job_id   = _preview_job_id_from_form()
    trip_index_raw   = request.form.get("trip_index", "0")
    try:
        trip_index = int(trip_index_raw)
    except (ValueError, TypeError):
        trip_index = 0

    def _load_job(job_id: str):
        job = report_jobs.get_job(job_id)
        if not job:
            return None, None
        if (job["created_by"]
                and job["created_by"] != session.get("user_id")
                and session.get("role") not in ("admin", "super_admin")):
            abort(403)
        items = [
            it for it in report_jobs.get_job_items(job_id,
                                                    statuses=["done"])
            if it.get("computed")
        ]
        return job, items

    if preview_job_id:
        job, items = _load_job(preview_job_id)
        if job is None:
            raise ValueError("Preview job not found.")
        if not items:
            raise ValueError("No completed trips to export yet.")
        if trip_index < 0 or trip_index >= len(items):
            raise ValueError("Invalid trip selection.")
        reports = [it["computed"] for it in items]
        return reports[trip_index], reports, trip_index, preview_job_id, None

    if preview_id:
        job, items = _load_job(preview_id)
        if job is not None:
            if not items:
                raise ValueError("No completed trips to export yet.")
            if trip_index < 0 or trip_index >= len(items):
                raise ValueError("Invalid trip selection.")
            reports = [it["computed"] for it in items]
            return reports[trip_index], reports, trip_index, preview_id, None

    if not preview_id:
        raise ValueError("Preview not ready or expired. Please search again.")
    state = get_preview(preview_id)
    if not state or state["status"] != "ready":
        raise ValueError("Preview not ready or expired. Please search again.")
    if (state.get("created_by")
            and state["created_by"] != session.get("user_id")
            and session.get("role") not in ("admin", "super_admin")):
        abort(403)
    reports = state.get("reports") or []
    if trip_index < 0 or trip_index >= len(reports):
        raise ValueError("Invalid trip selection.")
    return reports[trip_index], reports, trip_index, None, preview_id


def _safe_find_trips(imei, begin, end):
    if is_rate_limited():
        raise RateLimitExceeded("rate limited (short-circuit before call)")
    return find_trips(imei, begin, end)


def _safe_compute_report(trip, include_history, alarms=None):
    if is_rate_limited():
        raise RateLimitExceeded("rate limited (short-circuit before call)")
    t_start = datetime.strptime(
        trip["start"], "%Y-%m-%d %H:%M:%S"
    ).replace(tzinfo=timezone.utc)
    t_end = datetime.strptime(
        trip["end"], "%Y-%m-%d %H:%M:%S"
    ).replace(tzinfo=timezone.utc)
    return compute_report(
        imei=trip["imei"],
        truck_id=trip["truck"],
        driver_name=trip.get("driver") or "",
        begin=t_start, end=t_end,
        include_history=include_history,
        alarms=alarms,
    )


def _prefetch_alarms_for_preview_lazy(trips, include_history, *, max_workers=4):
    from jimi import fetch_alarms_chunked

    if not trips:
        return {}, {}

    per_imei_bounds = {}
    for t in trips:
        imei = t["imei"]
        t_start = datetime.strptime(
            t["start"], "%Y-%m-%d %H:%M:%S"
        ).replace(tzinfo=timezone.utc)
        t_end = datetime.strptime(
            t["end"], "%Y-%m-%d %H:%M:%S"
        ).replace(tzinfo=timezone.utc)
        if imei not in per_imei_bounds:
            per_imei_bounds[imei] = (t_start, t_end)
        else:
            b, e = per_imei_bounds[imei]
            per_imei_bounds[imei] = (min(b, t_start), max(e, t_end))

    expanded = {}
    for imei, (b, e) in per_imei_bounds.items():
        if include_history:
            hist_begin, hist_end = history_window(b)
            expanded[imei] = (min(b, hist_begin), max(e, hist_end))
        else:
            expanded[imei] = (b - YARD_LOOKBACK, e)

    def _fetch_one(imei):
        b, e = expanded[imei]
        try:
            return imei, fetch_alarms_chunked(imei, b, e)
        except RateLimitExceeded:
            raise
        except Exception as exc:
            log.warning("preview: alarm prefetch failed for %s: %s", imei, exc)
            return imei, []

    if is_rate_limited():
        raise RateLimitExceeded("rate limited (before alarm prefetch)")

    with ThreadPoolExecutor(max_workers=max_workers,
                            thread_name_prefix="preview-alarms") as pool:
        results = list(pool.map(_fetch_one, list(expanded.keys())))

    return dict(results), expanded


def _run_preview_single(preview_id, trip, include_history):
    try:
        t_start = datetime.strptime(
            trip["start"], "%Y-%m-%d %H:%M:%S"
        ).replace(tzinfo=timezone.utc)
        t_end = datetime.strptime(
            trip["end"], "%Y-%m-%d %H:%M:%S"
        ).replace(tzinfo=timezone.utc)
        computed = compute_report(
            imei=trip["imei"],
            truck_id=trip["truck"],
            driver_name=trip.get("driver") or "",
            begin=t_start, end=t_end,
            include_history=include_history,
        )
        mark_ready_reports(preview_id, [computed])
    except RateLimitExceeded as exc:
        log.warning("1-trip preview hit rate limit: %s", exc)
        mark_preview_failed(preview_id, "Tracksolid Pro daily API limit reached.")
    except Exception as exc:
        log.exception("1-trip preview failed")
        mark_preview_failed(preview_id, f"{type(exc).__name__}: {exc}")


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


_nav_alert_cache = {"value": 0, "expires_at": 0.0}
_nav_jobs_cache  = {"user_id": None, "value": 0, "expires_at": 0.0}
_NAV_CACHE_TTL = 10.0


@app.context_processor
def _inject_nav_context():
    ctx = {"nav_unacked_alerts": 0, "nav_pending_jobs": 0}
    if not session.get("user_id"):
        return ctx
    try:
        now = time.time()

        if session.get("role") == "super_admin":
            if now >= _nav_alert_cache["expires_at"]:
                _nav_alert_cache["value"] = audit.unacked_alert_count()
                _nav_alert_cache["expires_at"] = now + _NAV_CACHE_TTL
            ctx["nav_unacked_alerts"] = _nav_alert_cache["value"]

        uid = session.get("user_id")
        if (_nav_jobs_cache["user_id"] == uid
                and now < _nav_jobs_cache["expires_at"]):
            ctx["nav_pending_jobs"] = _nav_jobs_cache["value"]
        else:
            ctx["nav_pending_jobs"] = jobs.count_pending(uid)
            _nav_jobs_cache["user_id"] = uid
            _nav_jobs_cache["value"] = ctx["nav_pending_jobs"]
            _nav_jobs_cache["expires_at"] = now + _NAV_CACHE_TTL
    except Exception:
        pass
    return ctx


# --- routes ---------------------------------------------------------------

@app.route("/favicon.ico")
def favicon():
    return send_from_directory(app.static_folder, "gbc_logo.png",
                               mimetype="image/png")


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


@app.route("/api/usage")
@login_required
def api_usage():
    """Detailed usage view. Available to any signed-in user so anyone can
    check whether they're burning through the quota, but the /admin/usage
    page (where the reset button lives) is super-admin-only."""
    s = ratelimit_status()
    # Add a friendly summary that the nav / usage page can render directly.
    calls = s.get("calls", {})
    total = calls.get("total", 0)
    location = calls.get("location_calls", 0)
    quota = calls.get("quota_total", 8640)
    quota_loc = calls.get("quota_location", 86400)

    def level(percent: float) -> str:
        if percent >= 90:
            return "critical"
        if percent >= 75:
            return "warning"
        return "ok"

    s["summary"] = {
        "level_total":    level(calls.get("percent_total", 0)),
        "level_location": level(calls.get("percent_location", 0)),
        "message": (
            f"{total:,} of {quota:,} calls used"
            + (f" ({location:,}/{quota_loc:,} location)" if location else "")
        ),
    }
    return jsonify(s)


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
                f"include_history={include_history}, "
                f"workers={_TRIPS_WORKERS}"),
        meta={"truck_count": len(trucks), "include_history": include_history,
              "begin": begin_s, "end": end_s, "workers": _TRIPS_WORKERS},
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

    groups: list[dict] = []
    total  = 0
    rate_limited = False
    fetch_errors: list[str] = []

    future_to_dev = {
        _trips_pool.submit(_safe_find_trips, dev["imei"], begin, end): dev
        for dev in resolved
    }

    try:
        for future in as_completed(future_to_dev):
            dev = future_to_dev[future]
            try:
                trips = future.result()
            except RateLimitExceeded as exc:
                log.warning("find_trips rate limited for %s: %s",
                            dev["imei"], exc)
                rate_limited = True
                break
            except Exception as exc:
                log.exception("find_trips failed for %s", dev["imei"])
                fetch_errors.append(f"{dev['truck']}: {type(exc).__name__}")
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
    finally:
        if rate_limited:
            for f in future_to_dev:
                if not f.done():
                    f.cancel()

    if rate_limited:
        flash("Tracksolid Pro daily API limit reached. Try again later.", "error")
        return redirect(url_for("index"))

    if not groups:
        if fetch_errors:
            preview = ", ".join(fetch_errors[:5])
            more = f" (and {len(fetch_errors) - 5} more)" if len(fetch_errors) > 5 else ""
            flash(f"No trips found. Some trucks failed to load: {preview}{more}.",
                  "error")
        else:
            flash("No trips found for any of the selected trucks in that window.",
                  "error")
        return redirect(url_for("index"))

    order = {dev["imei"]: i for i, dev in enumerate(resolved)}
    groups.sort(key=lambda g: order.get(g["imei"], 10**9))

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

    audit.record(
        "preview.start",
        target=", ".join(sorted({s["truck"] for s in selected}))[:200],
        detail=(f"trips={len(selected)}, include_history={include_history}, "
                f"mode={'sync' if len(selected) == 1 else 'job'}"),
        meta={"trip_count": len(selected),
              "include_history": include_history},
    )

    if len(selected) == 1:
        trip = selected[0]
        preview_id = uuid.uuid4().hex[:12]
        store_preview(preview_id, {
            "status":          "computing",
            "queued_at":       datetime.now(timezone.utc),
            "truck":           trip["truck"],
            "imei":            trip["imei"],
            "driver_name":     trip.get("driver") or "",
            "trips":           selected,
            "reports":         None,
            "error":           None,
            "created_by":      session.get("user_id"),
            "include_history": include_history,
        })
        _preview_pool.submit(_run_preview_single,
                             preview_id, trip, include_history)
        return redirect(url_for("preview_show", preview_id=preview_id))

    job_id = report_jobs.create_preview_job(
        trips=[{
            "imei":   s["imei"],
            "truck":  s["truck"],
            "driver": s.get("driver") or "",
            "start":  s["start"],
            "end":    s["end"],
        } for s in selected],
        include_history=include_history,
        created_by=session.get("user_id"),
        created_by_email=session.get("email"),
    )
    return redirect(url_for("preview_job_show", job_id=job_id))


@app.route("/preview/job/<job_id>")
@login_required
def preview_job_show(job_id):
    job = report_jobs.get_job(job_id)
    if not job:
        flash("Preview job not found.", "error")
        return redirect(url_for("index"))
    if (job["created_by"]
            and job["created_by"] != session.get("user_id")
            and session.get("role") not in ("admin", "super_admin")):
        abort(403)

    items = report_jobs.get_job_items(job_id)

    total = job["total_items"]
    done = job["done_items"]
    failed = job["failed_items"]
    pending = total - done - failed
    pct = int(100 * (done + failed) / total) if total else 100

    return render_template(
        "preview_job.html",
        job=job,
        items=items,
        total=total, done=done, failed=failed, pending=pending, pct=pct,
        is_finished=(job["status"] in ("done", "failed", "cancelled")),
        can_export=(job["status"] == "done" and done > 0),
    )


@app.route("/api/preview/job/<job_id>")
@login_required
def api_preview_job(job_id):
    job = report_jobs.get_job(job_id)
    if not job:
        abort(404)
    if (job["created_by"]
            and job["created_by"] != session.get("user_id")
            and session.get("role") not in ("admin", "super_admin")):
        abort(403)

    items = report_jobs.get_job_items(job_id)
    return jsonify({
        "job":  job,
        "items": [{
            "id":         it["id"],
            "truck_id":   it["truck_id"],
            "imei":       it["imei"],
            "trip_start": it["trip_start"],
            "trip_end":   it["trip_end"],
            "status":     it["status"],
            "error":      it["error"],
        } for it in items],
    })


@app.route("/preview/job/<job_id>/trip/<item_id>")
@login_required
def preview_job_trip(job_id, item_id):
    job = report_jobs.get_job(job_id)
    if not job:
        flash("Preview job not found.", "error")
        return redirect(url_for("index"))
    if (job["created_by"]
            and job["created_by"] != session.get("user_id")
            and session.get("role") not in ("admin", "super_admin")):
        abort(403)

    item = report_jobs.get_job_item(job_id, item_id)
    if not item or not item.get("computed"):
        flash("Trip not ready yet.", "error")
        return redirect(url_for("preview_job_show", job_id=job_id))

    all_done = [
        it for it in report_jobs.get_job_items(job_id, statuses=["done"])
        if it.get("computed")
    ]
    reports = [it["computed"] for it in all_done]
    try:
        current_index = next(
            i for i, it in enumerate(all_done) if it["id"] == item_id
        )
    except StopIteration:
        reports = [item["computed"]]
        current_index = 0

    return render_template(
        "preview.html",
        loading=False, view_mode=False, is_bulk=(len(reports) > 1),
        preview_id=None,
        state={"truck": item["truck_id"], "imei": item["imei"]},
        reports=reports,
        current_index=current_index,
        computed=reports[current_index],
        manual={},
        report_id=None,
        job_id=job_id,
        item_id=item_id,
    )


@app.route("/preview/job/<job_id>/cancel", methods=["POST"])
@login_required
def preview_job_cancel(job_id):
    job = report_jobs.get_job(job_id)
    if not job:
        flash("Preview job not found.", "error")
        return redirect(url_for("index"))
    if (job["created_by"]
            and job["created_by"] != session.get("user_id")
            and session.get("role") not in ("admin", "super_admin")):
        abort(403)
    if job["status"] in ("done", "failed", "cancelled"):
        flash("Job already finished.", "info")
        return redirect(url_for("preview_job_show", job_id=job_id))

    report_jobs.mark_job_cancelled(job_id)
    audit.record("preview.cancel", target=job_id,
                 detail=f"cancelled with {job['done_items']}/{job['total_items']} done")
    flash("Preview cancelled.", "success")
    return redirect(url_for("preview_job_show", job_id=job_id))


@app.route("/preview/job/<job_id>/retry", methods=["POST"])
@login_required
def preview_job_retry(job_id):
    job = report_jobs.get_job(job_id)
    if not job:
        flash("Preview job not found.", "error")
        return redirect(url_for("index"))
    if (job["created_by"]
            and job["created_by"] != session.get("user_id")
            and session.get("role") not in ("admin", "super_admin")):
        abort(403)

    retried = report_jobs.requeue_failed_items(job_id)
    if not retried:
        flash("No failed trips to retry.", "info")
        return redirect(url_for("preview_job_show", job_id=job_id))

    audit.record("preview.retry", target=job_id,
                 detail=f"retrying {retried} failed items")
    flash(f"Retrying {retried} failed trip(s).", "success")
    return redirect(url_for("preview_job_show", job_id=job_id))


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
    try:
        computed, reports, trip_index, preview_job_id, preview_id = (
            _resolve_export_context()
        )
    except ValueError as exc:
        flash(str(exc), "error")
        if request.form.get("preview_job_id"):
            return redirect(url_for("preview_job_show",
                                    job_id=request.form["preview_job_id"]))
        return redirect(url_for("index"))

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
        if preview_job_id:
            return redirect(url_for("preview_job_show",
                                    job_id=preview_job_id))
        if preview_id:
            return redirect(url_for("preview_show", preview_id=preview_id,
                                    trip=trip_index))
        return redirect(url_for("index"))

    if preview_job_id:
        try:
            admin_client().table("reports").update({
                "error": preview_job_id,
            }).eq("id", job_id).execute()
        except Exception:
            log.exception("could not stash preview_job_id on report %s",
                          job_id)

    executor.submit(_run_export, job_id, computed, out_path, manual)
    audit.record(
        "report.export",
        target=computed["truck_id"],
        detail=(f"filename={filename}, trip_index={trip_index}, "
                f"trip_start={utc_str(computed['trip_start']) if computed['trip_start'] else ''}"),
        meta={"job_id": job_id, "filename": filename,
              "trip_index": trip_index, "imei": computed["imei"],
              "manual_keys": list(manual.keys()),
              "preview_job_id": preview_job_id},
    )
    flash(f"Export queued for {computed['truck_id']} "
          f"(trip {trip_index + 1} of {len(reports)}). "
          f"Download from the Reports page.", "success")

    if preview_job_id:
        return redirect(url_for("preview_job_show", job_id=preview_job_id))
    return redirect(url_for("reports"))


@app.route("/export/job", methods=["POST"])
@login_required
def export_job_start():
    preview_job_id = (request.form.get("preview_job_id") or "").strip()
    preview_job = report_jobs.get_job(preview_job_id)
    if not preview_job or preview_job["kind"] != "preview":
        flash("Preview job not found.", "error")
        return redirect(url_for("index"))
    if (preview_job["created_by"]
            and preview_job["created_by"] != session.get("user_id")
            and session.get("role") not in ("admin", "super_admin")):
        abort(403)
    if preview_job["status"] != "done":
        flash("Preview is not finished yet.", "error")
        return redirect(url_for("preview_job_show", job_id=preview_job_id))

    done_items = report_jobs.get_job_items(preview_job_id, statuses=["done"])
    done_items = [it for it in done_items if it.get("computed")]

    if not done_items:
        flash("No completed trips to export. Try retrying the failed "
              "items, or run the preview again.", "error")
        return redirect(url_for("preview_job_show", job_id=preview_job_id))

    done_reports = [it["computed"] for it in done_items]
    bulk_label = _bulk_truck_label(done_reports)
    safe_label = _bulk_safe_filename_label(done_reports)
    run_stamp  = datetime.now(timezone.utc).strftime("%H%M%S")
    today      = datetime.now(timezone.utc).date()
    filename   = f"Trip Review - {safe_label} - {today} - {run_stamp}.xlsx"

    def _as_dt(s):
        if not s:
            return None
        if isinstance(s, datetime):
            return s
        try:
            return datetime.fromisoformat(str(s).replace("Z", "+00:00"))
        except ValueError:
            return None

    starts = [d for d in (_as_dt(it["trip_start"]) for it in done_items) if d]
    ends   = [d for d in (_as_dt(it["trip_end"])   for it in done_items) if d]
    job_trip_start = min(starts) if starts else None
    job_trip_end   = max(ends)   if ends   else None

    try:
        reports_row_id = jobs.add_job(
            truck_id=bulk_label, imei=None, filename=filename,
            trip_start=job_trip_start, trip_end=job_trip_end,
            created_by=session.get("user_id"),
            created_by_email=session.get("email"),
            manual={"preview_job_id": preview_job_id},
        )
    except Exception as exc:
        log.exception("could not create reports row for export")
        flash(f"Could not queue bulk export: {exc}", "error")
        return redirect(url_for("preview_job_show", job_id=preview_job_id))

    client = admin_client()
    client.table("reports").update({
        "error": preview_job_id,
    }).eq("id", reports_row_id).execute()

    export_job_id = report_jobs.create_export_job(
        preview_job_id=preview_job_id,
        reports_row_id=reports_row_id,
        created_by=session.get("user_id"),
        created_by_email=session.get("email"),
    )

    audit.record("export.job.start", target=preview_job_id,
                 detail=f"export job {export_job_id}, "
                        f"{len(done_items)} trips, filename={filename}")
    return redirect(url_for("export_job_show", job_id=export_job_id))


@app.route("/export/job/<job_id>")
@login_required
def export_job_show(job_id):
    job = report_jobs.get_job(job_id)
    if not job or job["kind"] != "export":
        flash("Export job not found.", "error")
        return redirect(url_for("index"))
    if (job["created_by"]
            and job["created_by"] != session.get("user_id")
            and session.get("role") not in ("admin", "super_admin")):
        abort(403)
    return render_template("export_job.html", job=job)


@app.route("/api/export/job/<job_id>")
@login_required
def api_export_job(job_id):
    job = report_jobs.get_job(job_id)
    if not job:
        abort(404)
    if (job["created_by"]
            and job["created_by"] != session.get("user_id")
            and session.get("role") not in ("admin", "super_admin")):
        abort(403)
    return jsonify({"job": job})


@app.route("/reports")
@login_required
def reports():
    is_admin = session.get("role") in ("admin", "super_admin")
    jobs_list = jobs.all_jobs_sorted_for(
        session.get("user_id"), show_all=is_admin
    )
    return render_template("reports.html", jobs=jobs_list, is_admin=is_admin)


# --- report detail views --------------------------------------------------

def _load_report_or_404(report_id):
    row = reports_repo.get_full(report_id)
    if not row:
        flash("Report not found.", "error")
        return None
    owner = row.get("created_by")
    if (owner
            and owner != session.get("user_id")
            and session.get("role") not in ("admin", "super_admin")):
        abort(403)
    return row


@app.route("/reports/<report_id>/view")
@login_required
def report_view(report_id):
    """Single-trip report detail, or the per-trip page of a bulk report
    (when ?trip=N is present). For a bulk report with no ?trip=N, this
    redirects to the bulk index page (/reports/<id>/bulk)."""
    row = _load_report_or_404(report_id)
    if row is None:
        return redirect(url_for("reports"))

    stored = row.get("computed_json")
    if not stored:
        # Old bulk rows written before computed_json was persisted.
        # Fall back to the preview job if it still exists.
        preview_job_id = (row.get("error") or "").strip()
        if preview_job_id and report_jobs.get_job(preview_job_id):
            return redirect(url_for("preview_job_show",
                                    job_id=preview_job_id))
        flash("This report has no stored detail — regenerate it to view.",
              "error")
        return redirect(url_for("reports"))

    stored = _from_jsonable(stored)
    is_bulk = _is_bulk_payload(stored)
    reports = stored if is_bulk else [stored]
    if not reports:
        flash("This report is empty.", "error")
        return redirect(url_for("reports"))

    # If this is a bulk report and the caller didn't ask for a specific
    # trip, show the bulk index (table + search + filter) instead of a
    # single-trip detail page.
    if is_bulk and "trip" not in request.args:
        return redirect(url_for("report_bulk_view", report_id=report_id))

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


@app.route("/reports/<report_id>/bulk")
@login_required
def report_bulk_view(report_id):
    """Bulk report index: every trip in the workbook listed in a table,
    with search + status filter. Each row links to the per-trip detail
    (/reports/<id>/view?trip=N)."""
    row = _load_report_or_404(report_id)
    if row is None:
        return redirect(url_for("reports"))

    stored = row.get("computed_json")
    if not stored:
        flash("This report has no stored detail — regenerate it to view.",
              "error")
        return redirect(url_for("reports"))

    stored = _from_jsonable(stored)
    if not _is_bulk_payload(stored):
        # Single-trip report — redirect to the normal detail view.
        return redirect(url_for("report_view", report_id=report_id))

    reports = stored or []
    if not reports:
        flash("This report is empty.", "error")
        return redirect(url_for("reports"))

    items = []
    for i, r in enumerate(reports):
        items.append({
            "index":      i,
            "truck_id":   r.get("truck_id") or "",
            "imei":       r.get("imei") or "",
            "trip_start": r.get("trip_start"),
            "trip_end":   r.get("trip_end"),
        })

    trucks = sorted({it["truck_id"] for it in items if it["truck_id"]})

    if report_id:
        audit.record(
            "report.bulk_view",
            target=row.get("truck_id") or report_id,
            detail=f"filename={row.get('filename')}, trips={len(items)}",
            meta={"report_id": report_id, "trips": len(items)},
        )

    return render_template(
        "report_bulk.html",
        row=row,
        report_id=report_id,
        items=items,
        trucks=trucks,
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
            # Send the user back to the bulk index so they can pick the
            # next trip to edit.
            return redirect(url_for("report_bulk_view",
                                    report_id=report_id))
        flash("Report details updated and Excel regenerated.", "success")
    except Exception as exc:
        log.exception("edit-meta failed")
        flash(f"Could not update report: {exc}", "error")
        return redirect(url_for("report_view", report_id=report_id,
                                trip=trip_index))

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


try:
    job_worker.start()
except Exception as exc:
    log.error("could not start job worker: %s", exc)

# ---------------------------------------------------------------------------
# Worker startup
# ---------------------------------------------------------------------------
#
# On Windows + Flask debug mode, `WERKZEUG_RUN_MAIN=true` marks the
# reloader child (the process that actually serves requests). Without
# this check, the parent also starts the worker, giving 2× threads
# against the shared Supabase client — which is what was causing the
# `WinError 10035` socket errors. Under gunicorn (wsgi:app) there is
# only one process, so start unconditionally.

def _start_worker():
    try:
        job_worker.start()
        log.info("job worker thread started")
    except Exception as exc:
        log.error("could not start job worker: %s", exc)


if __name__ == "__main__":
    if os.environ.get("WERKZEUG_RUN_MAIN") == "true" or not FLASK_DEBUG:
        _start_worker()
    app.run(debug=FLASK_DEBUG, host="0.0.0.0", port=5000)
else:
    _start_worker()