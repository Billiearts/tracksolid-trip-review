"""
services/jobs.py — orchestrator for report rows.

Combines Postgres-backed reports (source of truth) with any legacy .xlsx
files on disk that don't have a matching Postgres row. Legacy files are
still shown to everyone, but new reports always go through Postgres.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path

from config import OUTPUT_DIR
from services import reports_repo


_TRIP_REVIEW_RE = re.compile(
    r"^Trip Review\s*-\s*(?P<truck>.+?)\s*-\s*"
    r"(?P<date>\d{4}-\d{2}-\d{2})\s*-\s*(?P<stamp>\d{6})\.xlsx$"
)


# ---------------------------------------------------------------------------
# Write API (used by app.py)
# ---------------------------------------------------------------------------

def add_job(*, truck_id: str, imei: str | None, filename: str,
            trip_start: datetime | None, trip_end: datetime | None,
            created_by: str | None, created_by_email: str | None) -> str:
    """Insert a queued row. Returns the row id (uuid string)."""
    return reports_repo.insert_queued(
        filename=filename,
        truck_id=truck_id,
        imei=imei,
        trip_start=trip_start,
        trip_end=trip_end,
        created_by=created_by,
        created_by_email=created_by_email,
    )


def mark_running(report_id: str) -> None:
    reports_repo.mark_running(report_id)


def mark_done(report_id: str, filesize_bytes: int | None = None) -> None:
    reports_repo.mark_done(report_id, filesize_bytes=filesize_bytes)


def mark_failed(report_id: str, error: str) -> None:
    reports_repo.mark_failed(report_id, error=error)


def sweep_stale() -> int:
    return reports_repo.sweep_stale()


# ---------------------------------------------------------------------------
# Read API
# ---------------------------------------------------------------------------

def _read_sidecar(xlsx_path: Path) -> dict | None:
    """Legacy .meta.json read (kept so old disk files still show truck/date)."""
    mp = xlsx_path.with_suffix(".meta.json")
    if not mp.exists():
        return None
    try:
        return json.loads(mp.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _disk_legacy_reports(known: set[str]) -> list[dict]:
    """Every .xlsx on disk that has no Postgres row. Shown to everyone."""
    out = []
    for p in sorted(OUTPUT_DIR.glob("*.xlsx"),
                    key=lambda p: p.stat().st_mtime, reverse=True):
        if p.name in known:
            continue
        mtime = datetime.fromtimestamp(p.stat().st_mtime, tz=timezone.utc)
        meta = _read_sidecar(p)
        if meta:
            truck = meta.get("truck_id") or "(unknown)"
            imei = meta.get("imei") or ""
            trip_start = meta.get("trip_start")
            trip_end = meta.get("trip_end")
        else:
            m = _TRIP_REVIEW_RE.match(p.name)
            truck = m.group("truck") if m else p.name
            imei = ""
            trip_start = trip_end = None
        out.append({
            "id":               None,
            "truck":            truck,
            "imei":             imei,
            "status":           "done",
            "filename":         p.name,
            "queued_at":        mtime,
            "started_at":       mtime,
            "finished_at":      mtime,
            "error":            None,
            "trip_start":       trip_start,
            "trip_end":         trip_end,
            "created_by":       None,
            "created_by_email": None,
            "filesize_bytes":   p.stat().st_size,
            "from_disk":        True,
        })
    return out


def all_jobs_sorted_for(user_id: str | None, show_all: bool) -> list[dict]:
    """Postgres rows (filtered) + legacy disk files (always visible to all)."""
    db_rows = reports_repo.list_for(user_id, show_all)
    known   = {r["filename"] for r in db_rows if r.get("filename")}
    # Legacy disk files never have an owner, so only include them in the
    # "show all" scope. Otherwise they'd leak across users.
    legacy  = _disk_legacy_reports(known) if show_all else []

    merged = db_rows + legacy
    merged.sort(
        key=lambda j: j["queued_at"] or datetime.min.replace(tzinfo=timezone.utc),
        reverse=True,
    )
    return merged


def get_report_owner(filename: str) -> str | None:
    """Return the user_id that owns a report file, or None if unknown.

    Reads Postgres first (canonical), falls back to the sidecar for legacy
    files, returns None if nothing is found.
    """
    row = reports_repo.get_by_filename(filename)
    if row is not None:
        return row.get("created_by")

    sidecar = _read_sidecar(OUTPUT_DIR / filename)
    if sidecar:
        return sidecar.get("created_by")
    return None


def all_jobs_sorted() -> list[dict]:
    """Everything — used by nothing currently, kept for compatibility."""
    return all_jobs_sorted_for(user_id=None, show_all=True)