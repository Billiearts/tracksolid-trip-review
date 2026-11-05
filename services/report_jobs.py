"""
services/report_jobs.py — CRUD for report_jobs / report_job_items.

A "job" is one of:
  kind='preview' — a batch of trips to compute. When it's done, the user
                   can view them, retry failures, or export.
  kind='export'  — a request to build one .xlsx containing every trip
                   from a completed preview job.

Jobs are persistent. A worker restart doesn't lose them: items stuck in
'status=running' with a stale heartbeat are re-queued by the next worker
on startup (see services/job_worker.py).

Public API:
    create_preview_job(...)         -> str (job_id)
    create_export_job(...)          -> str (job_id)
    get_job(job_id)                 -> dict | None
    get_job_items(job_id)           -> list[dict]
    get_job_item(job_id, item_id)   -> dict | None
    next_queued_items(job_id, n)    -> list[dict]      # for the worker
    mark_job_running(job_id)
    mark_job_done(job_id)
    mark_job_failed(job_id, error)
    mark_job_cancelled(job_id)
    touch_job_heartbeat(job_id)
    mark_item_running(item_id)
    mark_item_done(item_id, computed)
    mark_item_failed(item_id, error)
    requeue_failed_items(job_id)                     -> int
    requeue_stale_running(max_stale_minutes=5)        # startup recovery
    pending_items_for_user(user_id)                  -> int
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from services.supabase_client import admin_client


log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# JSON serialisation helpers
#
# `compute_report()` returns a dict that contains raw `datetime` objects
# (query_begin, query_end, trip_start, trip_end, generated_at, plus
# datetimes nested inside stops.intervals, yard.visit, history windows,
# etc.). The Supabase/postgrest client hands its payload straight to
# httpx, which calls json.dumps() — and json.dumps() cannot serialise a
# datetime. The result was a `TypeError: Object of type datetime is not
# JSON serializable` on every mark_item_done(), which the worker catches
# and logs, leaving done_items stuck at 0 and the preview page empty.
#
# `_to_jsonable` / `_from_jsonable` are the same pair used in app.py for
# the single-trip / bulk export path. Keep them in sync.
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_ts(s) -> datetime | None:
    if not s:
        return None
    if isinstance(s, datetime):
        return s if s.tzinfo else s.replace(tzinfo=timezone.utc)
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except ValueError:
        return None


def _row_to_job(row: dict) -> dict:
    return {
        "id":              row.get("id"),
        "created_by":      row.get("created_by"),
        "created_by_email": row.get("created_by_email"),
        "kind":            row.get("kind"),
        "status":          row.get("status"),
        "include_history": bool(row.get("include_history")),
        "total_items":     int(row.get("total_items") or 0),
        "done_items":      int(row.get("done_items") or 0),
        "failed_items":    int(row.get("failed_items") or 0),
        "reports_row_id":  row.get("reports_row_id"),
        "created_at":      _parse_ts(row.get("created_at")),
        "started_at":      _parse_ts(row.get("started_at")),
        "finished_at":     _parse_ts(row.get("finished_at")),
        "heartbeat_at":    _parse_ts(row.get("heartbeat_at")),
        "error":           row.get("error"),
    }


def _row_to_item(row: dict) -> dict:
    raw_computed = row.get("computed_json")
    return {
        "id":          row.get("id"),
        "job_id":      row.get("job_id"),
        "imei":        row.get("imei"),
        "truck_id":    row.get("truck_id"),
        "driver_name": row.get("driver_name") or "",
        "trip_start":  row.get("trip_start"),
        "trip_end":    row.get("trip_end"),
        "status":      row.get("status"),
        "error":       row.get("error"),
        "computed":    _from_jsonable(raw_computed)
                       if raw_computed is not None else None,
        "attempts":    int(row.get("attempts") or 0),
        "started_at":  _parse_ts(row.get("started_at")),
        "finished_at": _parse_ts(row.get("finished_at")),
    }


# Columns fetched by get_job_items / next_queued_items_for_job. Kept as a
# constant so we can't forget a column again: the previous version omitted
# `computed_json`, which made every item's `computed` field None even for
# `status='done'` rows — that's what caused "No completed trips to export"
# every time the user hit "Export all N trips".
_ITEM_COLUMNS = (
    "id,job_id,imei,truck_id,driver_name,trip_start,trip_end,"
    "status,error,attempts,started_at,finished_at,computed_json"
)


# ---------------------------------------------------------------------------
# Create
# ---------------------------------------------------------------------------

def create_preview_job(*, trips: list[dict],
                       include_history: bool,
                       created_by: str | None,
                       created_by_email: str | None) -> str:
    """Create a preview job and all of its items in one transaction.

    `trips` is a list of {imei, truck, driver, start, end} dicts from the
    /preview form. The items are inserted in a single Supabase call so
    partial failure can't leave a job with a wrong total_items count.

    Returns the job id.
    """
    if not trips:
        raise ValueError("create_preview_job: trips list is empty")

    client = admin_client()
    now = _now_iso()

    job_resp = client.table("report_jobs").insert({
        "created_by":       created_by,
        "created_by_email": created_by_email,
        "kind":             "preview",
        "status":           "queued",
        "include_history":  include_history,
        "total_items":      len(trips),
    }).execute()
    job_rows = job_resp.data or []
    if not job_rows:
        raise RuntimeError("create_preview_job: no row returned")
    job_id = job_rows[0]["id"]

    # Insert items in chunks so we don't blow the request size for 400 trips.
    item_rows = []
    for t in trips:
        item_rows.append({
            "job_id":      job_id,
            "imei":        t["imei"],
            "truck_id":    t["truck"],
            "driver_name": t.get("driver") or "",
            "trip_start":  t["start"],
            "trip_end":    t["end"],
            "status":      "queued",
        })

    CHUNK = 100
    for i in range(0, len(item_rows), CHUNK):
        client.table("report_job_items").insert(
            item_rows[i:i + CHUNK]
        ).execute()

    log.info("created preview job %s with %d items", job_id, len(item_rows))
    return job_id


def create_export_job(*, preview_job_id: str,
                      reports_row_id: str,
                      created_by: str | None,
                      created_by_email: str | None) -> str:
    """Create an export job that will build one workbook from every trip
    in the (completed) preview job `preview_job_id`.

    `reports_row_id` is the row in `reports` that will hold the exported
    file. The export worker fills it in when done.
    """
    client = admin_client()
    resp = client.table("report_jobs").insert({
        "created_by":       created_by,
        "created_by_email": created_by_email,
        "kind":             "export",
        "status":           "queued",
        "include_history":  False,     # irrelevant for export, always false
        "total_items":      1,         # one item per export job (the whole file)
        "reports_row_id":   reports_row_id,
    }).execute()
    rows = resp.data or []
    if not rows:
        raise RuntimeError("create_export_job: no row returned")
    return rows[0]["id"]


# ---------------------------------------------------------------------------
# Read
# ---------------------------------------------------------------------------

def get_job(job_id: str) -> dict | None:
    client = admin_client()
    try:
        resp = (client.table("report_jobs")
                .select("*")
                .eq("id", job_id)
                .limit(1)
                .execute())
    except Exception as exc:
        log.warning("get_job(%s) failed: %s", job_id, exc)
        return None
    rows = resp.data or []
    return _row_to_job(rows[0]) if rows else None


def get_job_items(job_id: str,
                  statuses: list[str] | None = None) -> list[dict]:
    """Return every item for a job, optionally filtered by status.

    Selects `computed_json` so callers that need the report payload
    (notably `export_job_start`) actually get it. Omitting it here is
    what made `it["computed"]` None for every 'done' row.
    """
    client = admin_client()
    q = (client.table("report_job_items")
         .select(_ITEM_COLUMNS)
         .eq("job_id", job_id)
         .order("trip_start", desc=False))
    if statuses:
        q = q.in_("status", statuses)
    try:
        resp = q.execute()
    except Exception as exc:
        log.warning("get_job_items(%s) failed: %s", job_id, exc)
        return []
    return [_row_to_item(r) for r in (resp.data or [])]


def get_job_item(job_id: str, item_id: str) -> dict | None:
    client = admin_client()
    try:
        resp = (client.table("report_job_items")
                .select("*")
                .eq("job_id", job_id)
                .eq("id", item_id)
                .limit(1)
                .execute())
    except Exception as exc:
        log.warning("get_job_item(%s, %s) failed: %s", job_id, item_id, exc)
        return None
    rows = resp.data or []
    return _row_to_item(rows[0]) if rows else None


def pending_items_for_user(user_id: str) -> int:
    """Count queued/running preview jobs for a user. Used by the nav badge."""
    if not user_id:
        return 0
    client = admin_client()
    try:
        resp = (client.table("report_jobs")
                .select("id", count="exact")
                .eq("created_by", user_id)
                .eq("kind", "preview")
                .in_("status", ["queued", "running"])
                .execute())
        return int(getattr(resp, "count", 0) or 0)
    except Exception:
        return 0


# ---------------------------------------------------------------------------
# Job state transitions
# ---------------------------------------------------------------------------

def mark_job_running(job_id: str) -> None:
    client = admin_client()
    client.table("report_jobs").update({
        "status":       "running",
        "started_at":   _now_iso(),
        "heartbeat_at": _now_iso(),
    }).eq("id", job_id).execute()


def mark_job_done(job_id: str) -> None:
    client = admin_client()
    client.table("report_jobs").update({
        "status":      "done",
        "finished_at": _now_iso(),
    }).eq("id", job_id).execute()


def mark_job_failed(job_id: str, error: str) -> None:
    client = admin_client()
    client.table("report_jobs").update({
        "status":      "failed",
        "finished_at": _now_iso(),
        "error":       error[:2000],
    }).eq("id", job_id).execute()


def mark_job_cancelled(job_id: str) -> None:
    client = admin_client()
    client.table("report_jobs").update({
        "status":      "cancelled",
        "finished_at": _now_iso(),
    }).eq("id", job_id).execute()


def touch_job_heartbeat(job_id: str) -> None:
    client = admin_client()
    client.table("report_jobs").update({
        "heartbeat_at": _now_iso(),
    }).eq("id", job_id).execute()


def bump_job_counters(job_id: str,
                      *, done_delta: int = 0,
                      failed_delta: int = 0) -> None:
    """Increment done_items / failed_items atomically.

    Postgres doesn't have a single-call increment via the Supabase REST
    client, so we fetch + update. Because only one worker owns a job at
    a time (Option B), there's no race.
    """
    client = admin_client()
    resp = (client.table("report_jobs")
            .select("done_items,failed_items")
            .eq("id", job_id)
            .limit(1)
            .execute())
    rows = resp.data or []
    if not rows:
        return
    row = rows[0]
    new_done = int(row.get("done_items") or 0) + done_delta
    new_failed = int(row.get("failed_items") or 0) + failed_delta
    client.table("report_jobs").update({
        "done_items":   new_done,
        "failed_items": new_failed,
        "heartbeat_at": _now_iso(),
    }).eq("id", job_id).execute()


# ---------------------------------------------------------------------------
# Item state transitions
# ---------------------------------------------------------------------------

def mark_item_running(item_id: str) -> None:
    client = admin_client()
    client.table("report_job_items").update({
        "status":     "running",
        "started_at": _now_iso(),
    }).eq("id", item_id).execute()


def mark_item_done(item_id: str, computed: dict) -> None:
    client = admin_client()
    client.table("report_job_items").update({
        "status":        "done",
        "computed_json": _to_jsonable(computed),
        "finished_at":   _now_iso(),
        "error":         None,
    }).eq("id", item_id).execute()


def mark_item_failed(item_id: str, error: str) -> None:
    client = admin_client()
    client.table("report_job_items").update({
        "status":      "failed",
        "finished_at": _now_iso(),
        "error":       error[:2000],
    }).eq("id", item_id).execute()


def requeue_failed_items(job_id: str) -> int:
    """Reset every failed item in a job back to 'queued' and reopen the
    parent job so the worker picks it up again.

    Replaces the inline admin_client() juggling that used to live in the
    /preview/job/<id>/retry route (and which raised NameError when the
    import drifted). Returns the number of items re-queued.
    """
    client = admin_client()

    resp = (client.table("report_job_items")
            .select("id")
            .eq("job_id", job_id)
            .eq("status", "failed")
            .execute())
    ids = [r["id"] for r in (resp.data or [])]
    if not ids:
        return 0

    (client.table("report_job_items")
     .update({"status": "queued", "error": None,
              "started_at": None, "finished_at": None})
     .in_("id", ids)
     .execute())

    (client.table("report_jobs")
     .update({"status": "queued", "finished_at": None,
              "failed_items": 0})
     .eq("id", job_id)
     .execute())

    log.info("requeued %d failed item(s) for job %s", len(ids), job_id)
    return len(ids)


# ---------------------------------------------------------------------------
# Worker helpers
# ---------------------------------------------------------------------------

def claim_next_queued_job() -> dict | None:
    """Atomically claim the oldest queued job.

    Retries up to 3 times on transient WinError 10035 socket errors, which
    can still happen even with the thread-local Supabase client when
    several threads burst the API simultaneously. Other errors fall
    straight through.
    """
    client = admin_client()
    for _attempt in range(3):
        try:
            resp = (client.table("report_jobs")
                    .select("*")
                    .eq("status", "queued")
                    .order("created_at", desc=False)
                    .limit(1)
                    .execute())
        except Exception as exc:
            msg = str(exc)
            if ("WinError 10035" in msg
                    or "would block" in msg
                    or "ConnectionResetError" in msg):
                time.sleep(0.5)
                continue
            log.warning("claim_next_queued_job: select failed: %s", exc)
            return None
        rows = resp.data or []
        if not rows:
            return None
        job = rows[0]
        try:
            upd = (client.table("report_jobs")
                   .update({
                       "status":       "running",
                       "started_at":   _now_iso(),
                       "heartbeat_at": _now_iso(),
                   })
                   .eq("id", job["id"])
                   .eq("status", "queued")
                   .execute())
        except Exception as exc:
            log.warning("claim_next_queued_job: update failed: %s", exc)
            return None
        if upd.data:
            return _row_to_job(upd.data[0])
    return None


def next_queued_items_for_job(job_id: str, n: int) -> list[dict]:
    """Return up to `n` queued items for the job, oldest first."""
    client = admin_client()
    try:
        resp = (client.table("report_job_items")
                .select("*")
                .eq("job_id", job_id)
                .eq("status", "queued")
                .order("id", desc=False)
                .limit(n)
                .execute())
    except Exception as exc:
        log.warning("next_queued_items_for_job(%s) failed: %s", job_id, exc)
        return []
    return [_row_to_item(r) for r in (resp.data or [])]


def requeue_stale_running(max_stale_minutes: int = 5) -> int:
    """On startup: any item that's been `running` for longer than the
    heartbeat window was orphaned by a crashed worker. Re-queue it.

    Also re-queues the parent job (status='running') if all of its items
    are queued again — otherwise it'd sit in 'running' forever.
    """
    client = admin_client()
    cutoff = (datetime.now(timezone.utc)
              - timedelta(minutes=max_stale_minutes)).isoformat()
    try:
        # Items stuck in 'running' with a start time before the cutoff.
        resp = (client.table("report_job_items")
                .select("id,job_id")
                .eq("status", "running")
                .lt("started_at", cutoff)
                .execute())
        stale = resp.data or []
        if not stale:
            return 0

        item_ids = [r["id"] for r in stale]
        job_ids  = sorted({r["job_id"] for r in stale})

        (client.table("report_job_items")
         .update({"status": "queued", "started_at": None})
         .in_("id", item_ids)
         .execute())

        # Reopen parent jobs.
        (client.table("report_jobs")
         .update({"status": "queued", "started_at": None})
         .in_("id", job_ids)
         .eq("status", "running")
         .execute())

        log.warning("requeued %d stale items across %d job(s)",
                    len(item_ids), len(job_ids))
        return len(item_ids)
    except Exception as exc:
        log.warning("requeue_stale_running failed: %s", exc)
        return 0