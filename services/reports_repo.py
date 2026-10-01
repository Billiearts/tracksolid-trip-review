"""
services/reports_repo.py — Postgres CRUD for the reports table.

Pure data access. Ownership rules and disk reconciliation live in
services/jobs.py.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from services.supabase_client import admin_client


log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _parse_ts(s) -> datetime | None:
    if s is None:
        return None
    if isinstance(s, datetime):
        return s if s.tzinfo else s.replace(tzinfo=timezone.utc)
    s2 = s.replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(s2)
    except ValueError:
        return None


def _row_to_dict(row: dict) -> dict:
    return {
        "id":               row["id"],
        "truck":            row.get("truck_id") or "(unknown)",
        "imei":             row.get("imei") or "",
        "status":           row.get("status") or "queued",
        "filename":         row.get("filename"),
        "queued_at":        _parse_ts(row.get("queued_at")),
        "started_at":       _parse_ts(row.get("started_at")),
        "finished_at":      _parse_ts(row.get("finished_at")),
        "error":            row.get("error"),
        "trip_start":       row.get("trip_start"),
        "trip_end":         row.get("trip_end"),
        "created_by":       row.get("created_by"),
        "created_by_email": row.get("created_by_email"),
        "filesize_bytes":   row.get("filesize_bytes"),
        "from_disk":        False,
    }


# ---------------------------------------------------------------------------
# Writes
# ---------------------------------------------------------------------------

def insert_queued(*, filename: str, truck_id: str, imei: str | None,
                  trip_start: datetime | None, trip_end: datetime | None,
                  created_by: str | None, created_by_email: str | None) -> str:
    client = admin_client()
    payload = {
        "filename":         filename,
        "truck_id":         truck_id,
        "imei":             imei,
        "status":           "queued",
        "trip_start":       trip_start.isoformat() if trip_start else None,
        "trip_end":         trip_end.isoformat()   if trip_end   else None,
        "created_by":       created_by,
        "created_by_email": created_by_email,
    }
    resp = client.table("reports").insert(payload).execute()
    rows = resp.data or []
    if not rows:
        raise RuntimeError("insert_queued: Supabase returned no row.")
    return rows[0]["id"]


def mark_running(report_id: str) -> None:
    client = admin_client()
    client.table("reports").update({
        "status":     "running",
        "started_at": datetime.now(timezone.utc).isoformat(),
    }).eq("id", report_id).execute()


def mark_done(report_id: str, filesize_bytes: int | None = None) -> None:
    client = admin_client()
    payload = {
        "status":      "done",
        "finished_at": datetime.now(timezone.utc).isoformat(),
        "error":       None,
    }
    if filesize_bytes is not None:
        payload["filesize_bytes"] = filesize_bytes
    client.table("reports").update(payload).eq("id", report_id).execute()


def mark_failed(report_id: str, error: str) -> None:
    client = admin_client()
    client.table("reports").update({
        "status":      "failed",
        "finished_at": datetime.now(timezone.utc).isoformat(),
        "error":       error,
    }).eq("id", report_id).execute()


def save_computed(report_id: str, data: dict) -> None:
    """Persist the full computed report as JSONB for later viewing."""
    client = admin_client()
    client.table("reports").update({
        "computed_json": data,
    }).eq("id", report_id).execute()


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------

def list_for(user_id: str | None, show_all: bool) -> list[dict]:
    client = admin_client()
    query = client.table("reports").select("*").order("queued_at", desc=True)
    if not show_all:
        if not user_id:
            return []
        query = query.eq("created_by", user_id)
    resp = query.execute()
    return [_row_to_dict(r) for r in (resp.data or [])]


def all_filenames() -> set[str]:
    client = admin_client()
    resp = client.table("reports").select("filename").execute()
    return {r["filename"] for r in (resp.data or []) if r.get("filename")}


def get_by_filename(filename: str) -> dict | None:
    client = admin_client()
    resp = (
        client.table("reports")
        .select("*")
        .eq("filename", filename)
        .limit(1)
        .execute()
    )
    rows = resp.data or []
    return _row_to_dict(rows[0]) if rows else None


def get_full(report_id: str) -> dict | None:
    """Fetch a raw report row including computed_json, or None."""
    client = admin_client()
    try:
        resp = (
            client.table("reports")
            .select("*")
            .eq("id", report_id)
            .limit(1)
            .execute()
        )
    except Exception as exc:
        log.warning("get_full failed for %s: %s", report_id, exc)
        return None
    rows = resp.data or []
    return rows[0] if rows else None


# ---------------------------------------------------------------------------
# Maintenance
# ---------------------------------------------------------------------------

def sweep_stale(minutes: int = 10) -> int:
    from datetime import timedelta
    cutoff = (datetime.now(timezone.utc) - timedelta(minutes=minutes)).isoformat()
    client = admin_client()
    resp = (
        client.table("reports")
        .update({
            "status":      "failed",
            "finished_at": datetime.now(timezone.utc).isoformat(),
            "error":       "Job interrupted (server restart or crash).",
        })
        .in_("status", ["queued", "running"])
        .lt("queued_at", cutoff)
        .execute()
    )
    return len(resp.data or [])