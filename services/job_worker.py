"""
services/job_worker.py — single daemon thread that processes report jobs.

Design:

  * One worker thread per gunicorn worker. On Render's free tier that's
    one thread (WEB_CONCURRENCY=1). If you scale to 2 gunicorn workers,
    you get 2 job threads — Option B, "aggressive". Each thread claims
    one job at a time via `claim_next_queued_job()`, so 2 gunicorn
    workers = 2 concurrent jobs.

  * Within a claimed job, the worker fans out `JOB_WORKERS` parallel
    `compute_report` calls (default 16 — "aggressive" per your call).
    Total concurrent Tracksolid calls = JOB_WORKERS × PREVIEW_WORKERS
    where PREVIEW_WORKERS is the internal history-fan-out inside
    compute_report. With both at 16 that's 256 in flight, which is too
    much. So the actual default is:

        JOB_WORKERS=16          (trips computed in parallel per job)
        HISTORY_WORKERS=4       (past trips per trip, inside build_history)

    Peak = 16 trips × 4 history calls = 64 concurrent Tracksolid calls.
    That's aggressive but not insane; if you see 1006s, drop JOB_WORKERS
    to 8 via env var.

  * On startup, `requeue_stale_running()` un-sticks any `running` items
    whose worker died. So a deploy mid-job doesn't lose progress.

  * Every 30 s the job's `heartbeat_at` is bumped. If the process dies
    without updating it, the next worker (or next request) treats the
    job as crashed and calls `requeue_stale_running()`.

The worker is started by `gunicorn.conf.py` → `post_fork`. It's also
started by `app.py` at import time for local dev (`flask run`).
"""
from __future__ import annotations

import logging
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

from jimi import RateLimitExceeded
from report import compute_report
from services import report_jobs
from services.ratelimit import is_rate_limited
from services.previews import (
    mark_ready_reports, mark_failed as mark_preview_failed,
)


log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

def _int_env(name: str, default: int, lo: int, hi: int) -> int:
    try:
        v = int(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        v = default
    return max(lo, min(hi, v))


# How many trips to compute in parallel within one job. Aggressive default;
# override with JOB_WORKERS=8 if you see 1006s.
JOB_WORKERS = _int_env("JOB_WORKERS", 16, 1, 32)

# How often the worker polls the DB for new queued jobs when idle.
POLL_INTERVAL_SEC = 5

# How often to bump the job's heartbeat while working.
HEARTBEAT_INTERVAL_SEC = 30


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------

_worker_thread: threading.Thread | None = None
_stop_event = threading.Event()


# ---------------------------------------------------------------------------
# Per-item compute (called from the JOB_WORKERS pool)
# ---------------------------------------------------------------------------

def _compute_item(item: dict,
                  include_history: bool,
                  prefetched_alarms: dict[str, list[dict]],
                  prefetch_bounds: dict[str, tuple[datetime, datetime]],
                  ) -> dict:
    """Compute one trip. Returns {item_id, status, computed, error}."""
    item_id = item["id"]
    imei    = item["imei"]
    trip_start = datetime.fromisoformat(
        item["trip_start"].replace("Z", "+00:00")
    )
    trip_end = datetime.fromisoformat(
        item["trip_end"].replace("Z", "+00:00")
    )

    if is_rate_limited():
        raise RateLimitExceeded("rate limited (short-circuit before compute)")

    alarms = prefetched_alarms.get(imei)
    pb, pe = prefetch_bounds.get(imei, (None, None))

    try:
        computed = compute_report(
            imei=imei,
            truck_id=item["truck_id"],
            driver_name=item.get("driver_name") or "",
            begin=trip_start,
            end=trip_end,
            include_history=include_history,
            alarms=alarms,
            prefetch_begin=pb,
            prefetch_end=pe,
        )
        return {"item_id": item_id, "status": "done", "computed": computed}
    except RateLimitExceeded:
        raise
    except Exception as exc:
        log.exception("compute failed for item %s (%s)", item_id, imei)
        return {"item_id": item_id, "status": "failed", "error": str(exc)}


# ---------------------------------------------------------------------------
# Preview job
# ---------------------------------------------------------------------------

def _run_preview_job(job: dict) -> None:
    job_id = job["id"]
    include_history = job["include_history"]

    log.info("worker: preview job %s starting (%d items)",
             job_id, job["total_items"])

    BATCH = JOB_WORKERS * 2   # fetch 2x workers at a time, so the pool stays busy

    heartbeat_lock = threading.Lock()
    last_heartbeat = {"t": 0.0}

    def _maybe_heartbeat():
        now = time.time()
        with heartbeat_lock:
            if now - last_heartbeat["t"] < HEARTBEAT_INTERVAL_SEC:
                return
            last_heartbeat["t"] = now
        try:
            report_jobs.touch_job_heartbeat(job_id)
        except Exception as exc:
            log.debug("heartbeat failed: %s", exc)

    from app import (
        _prefetch_alarms_for_preview_lazy,
    )
    items = report_jobs.get_job_items(job_id)
    trips = [{
        "imei":  it["imei"],
        "truck": it["truck_id"],
        "driver": it.get("driver_name") or "",
        "start": it["trip_start"],
        "end":   it["trip_end"],
    } for it in items]

    prefetched_alarms: dict[str, list[dict]] = {}
    prefetch_bounds: dict[str, tuple[datetime, datetime]] = {}
    if trips:
        try:
            prefetched_alarms, prefetch_bounds = (
                _prefetch_alarms_for_preview_lazy(
                    trips, include_history,
                    max_workers=JOB_WORKERS,
                )
            )
        except RateLimitExceeded as exc:
            log.warning("preview job %s alarm prefetch hit 1006: %s",
                        job_id, exc)
            prefetched_alarms, prefetch_bounds = {}, {}
        except Exception as exc:
            log.warning("preview job %s alarm prefetch failed: %s",
                        job_id, exc)
            prefetched_alarms, prefetch_bounds = {}, {}

    done = 0
    failed = 0

    with ThreadPoolExecutor(max_workers=JOB_WORKERS,
                            thread_name_prefix=f"job-{job_id[:8]}") as pool:
        while True:
            if _stop_event.is_set():
                log.info("worker: stopping before job %s (shutdown)", job_id)
                return

            current_job = report_jobs.get_job(job_id)
            if current_job is None or current_job["status"] == "cancelled":
                log.info("worker: job %s cancelled, stopping", job_id)
                return

            _maybe_heartbeat()

            batch = report_jobs.next_queued_items_for_job(job_id, BATCH)
            if not batch:
                break

            futures = {
                pool.submit(
                    _compute_item,
                    it, include_history,
                    prefetched_alarms, prefetch_bounds,
                ): it
                for it in batch
            }

            for fut in as_completed(futures):
                if _stop_event.is_set():
                    for f in futures:
                        f.cancel()
                    return

                try:
                    result = fut.result()
                except RateLimitExceeded as exc:
                    log.warning("rate limit hit mid-job %s: %s", job_id, exc)
                    for f in futures:
                        f.cancel()
                    report_jobs.mark_job_failed(
                        job_id,
                        "Tracksolid Pro daily API limit reached mid-job. "
                        "Completed trips are saved; retry after quota reset.",
                    )
                    return
                except Exception as exc:
                    log.exception("unexpected future failure: %s", exc)
                    continue

                item_id = result["item_id"]
                if result["status"] == "done":
                    try:
                        report_jobs.mark_item_done(item_id,
                                                   result["computed"])
                        report_jobs.bump_job_counters(job_id, done_delta=1)
                        done += 1
                    except Exception as exc:
                        log.exception("mark_item_done(%s) failed: %s",
                                      item_id, exc)
                else:
                    try:
                        report_jobs.mark_item_failed(item_id,
                                                     result["error"])
                        report_jobs.bump_job_counters(job_id,
                                                      failed_delta=1)
                        failed += 1
                    except Exception as exc:
                        log.exception("mark_item_failed(%s) failed: %s",
                                      item_id, exc)

    final = report_jobs.get_job(job_id)
    if final and final["status"] == "running":
        report_jobs.mark_job_done(job_id)
        log.info("worker: preview job %s done (%d ok, %d failed)",
                 job_id, done, failed)


# ---------------------------------------------------------------------------
# Export job
# ---------------------------------------------------------------------------

def _run_export_job(job: dict) -> None:
    """Build one workbook from a completed preview job.

    The `reports` row referenced by job['reports_row_id'] was created by
    the /export/job route before this job was queued, with status='queued'.
    On success:
      * the file is written and uploaded,
      * `computed_json` and `manual_json` are persisted on that row so
        the /reports/<id>/view page has data to render (previously this
        was skipped, which is why bulk reports showed "no stored detail"),
      * the row is marked 'done'.
    """
    from pathlib import Path
    from config import TEMPLATE_PATH, OUTPUT_DIR
    from report import write_bulk_excel
    from services import reports_repo, storage, jobs as jobs_service
    from services.report_jobs import _to_jsonable

    job_id = job["id"]
    reports_row_id = job.get("reports_row_id")
    if not reports_row_id:
        report_jobs.mark_job_failed(job_id, "export job has no reports row")
        return

    log.info("worker: export job %s starting (reports_row_id=%s)",
             job_id, reports_row_id)

    row = reports_repo.get_full(reports_row_id)
    if not row:
        report_jobs.mark_job_failed(job_id, "reports row not found")
        return

    # The preview job that this export refers to is stored in `error` as
    # a workaround — see app.py's /export/job route.
    try:
        preview_job_id = (row.get("error") or "").strip()
        if not preview_job_id:
            raise ValueError("no preview job id in reports.error")
        preview_items = report_jobs.get_job_items(preview_job_id,
                                                  statuses=["done"])
    except Exception as exc:
        report_jobs.mark_job_failed(job_id,
                                    f"could not load preview items: {exc}")
        return

    if not preview_items:
        report_jobs.mark_job_failed(job_id, "preview has no completed items")
        return

    # Reconstruct the reports list that write_bulk_excel expects. Skip
    # any item whose computed_json is NULL (pre-dates the serialisation
    # fix); including it would crash the template fill.
    reports = [
        it["computed"] for it in preview_items
        if it.get("computed")
    ]
    if not reports:
        report_jobs.mark_job_failed(job_id,
                                    "preview has no usable report data")
        return

    per_trip_manual = row.get("manual_json") or {}

    filename = row["filename"]
    out_path = OUTPUT_DIR / filename

    jobs_service.mark_running(reports_row_id)
    try:
        write_bulk_excel(reports, TEMPLATE_PATH, out_path,
                         per_trip_manual=per_trip_manual)
        size = out_path.stat().st_size if out_path.exists() else None
    except Exception as exc:
        log.exception("bulk export build failed for job %s", job_id)
        jobs_service.mark_failed(reports_row_id,
                                 error=f"{type(exc).__name__}: {exc}")
        report_jobs.mark_job_failed(job_id, str(exc))
        return

    try:
        storage.upload(out_path)
    except Exception:
        log.exception("bulk export upload raised for %s", out_path.name)

    # Persist the computed reports so /reports/<id>/view can render the
    # bulk report. This mirrors what _run_bulk_export in app.py does for
    # the synchronous bulk path. Skipping this is what made "View" on a
    # finished bulk export show "This report has no stored detail".
    try:
        reports_repo.save_computed(
            reports_row_id, _to_jsonable(list(reports))
        )
        reports_repo.update_manual(reports_row_id, per_trip_manual)
    except Exception:
        log.exception(
            "could not save computed/manual for bulk export %s "
            "(report will not be viewable, but the file is available)",
            reports_row_id,
        )

    jobs_service.mark_done(reports_row_id, filesize_bytes=size)
    report_jobs.mark_job_done(job_id)
    log.info("worker: export job %s done (%d trips, %d bytes)",
             job_id, len(reports), size or 0)


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

def _run_loop():
    log.info("job worker: started (JOB_WORKERS=%d)", JOB_WORKERS)

    try:
        n = report_jobs.requeue_stale_running()
        if n:
            log.info("job worker: requeued %d stale items on startup", n)
    except Exception as exc:
        log.warning("job worker: startup requeue failed: %s", exc)

    while not _stop_event.is_set():
        try:
            job = report_jobs.claim_next_queued_job()
        except Exception as exc:
            log.warning("job worker: claim failed: %s", exc)
            job = None

        if job is None:
            _stop_event.wait(POLL_INTERVAL_SEC)
            continue

        try:
            if job["kind"] == "preview":
                _run_preview_job(job)
            elif job["kind"] == "export":
                _run_export_job(job)
            else:
                log.warning("job worker: unknown kind %r for job %s",
                            job["kind"], job["id"])
                report_jobs.mark_job_failed(
                    job["id"], f"unknown kind: {job['kind']!r}"
                )
        except Exception as exc:
            log.exception("job worker: job %s crashed", job["id"])
            try:
                report_jobs.mark_job_failed(
                    job["id"], f"{type(exc).__name__}: {exc}"
                )
            except Exception:
                pass

    log.info("job worker: stopped")


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------

def start() -> None:
    """Start the worker thread. Idempotent."""
    global _worker_thread
    if _worker_thread is not None and _worker_thread.is_alive():
        return
    _stop_event.clear()
    _worker_thread = threading.Thread(
        target=_run_loop,
        name="report-job-worker",
        daemon=True,
    )
    _worker_thread.start()


def stop() -> None:
    """Signal the worker to stop at the next checkpoint."""
    _stop_event.set()