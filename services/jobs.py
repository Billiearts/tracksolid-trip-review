"""
services/jobs.py — orchestrator for report rows.
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
# Write API
# ---------------------------------------------------------------------------

def add_job(*, truck_id: str, imei: str | None, filename: str,
            trip_start: datetime | None, trip_end: datetime | None,
            created_by: str | None, created_by_email: str | None,
            manual: dict | None = None) -> str:
    return reports_repo.insert_queued(
        filename=filename,
        truck_id=truck_id,
        imei=imei,
        trip_start=trip_start,
        trip_end=trip_end,
        created_by=created_by,
        created_by_email=created_by_email,
        manual=manual,
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
    mp = xlsx_path.with_suffix(".meta.json")
    if not mp.exists():
        return None
    try:
        return json.loads(mp.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _disk_legacy_reports(known: set[str]) -> list[dict]:
    out = []
    try:
        paths = sorted(OUTPUT_DIR.glob("*.xlsx"),
                       key=lambda p: p.stat().st_mtime, reverse=True)
    except Exception:
        return out
    for p in paths:
        if p.name in known:
            continue
        try:
            st = p.stat()
        except OSError:
            continue
        mtime = datetime.fromtimestamp(st.st_mtime, tz=timezone.utc)
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
            "filesize_bytes":   st.st_size,
            "from_disk":        True,
        })
    return out


def all_jobs_sorted_for(user_id: str | None, show_all: bool) -> list[dict]:
    db_rows = reports_repo.list_for(user_id, show_all)
    known   = {r["filename"] for r in db_rows if r.get("filename")}
    legacy  = _disk_legacy_reports(known) if show_all else []

    merged = db_rows + legacy
    merged.sort(
        key=lambda j: j["queued_at"] or datetime.min.replace(tzinfo=timezone.utc),
        reverse=True,
    )
    return merged


def count_pending(user_id: str | None) -> int:
    """Number of queued/running jobs for a user (all jobs if user_id is None
    AND show_all was implied — this helper is used by the nav context, so
    non-admins only see their own)."""
    try:
        rows = reports_repo.list_for(user_id, show_all=False)
        return sum(1 for r in rows if r.get("status") in ("queued", "running"))
    except Exception:
        return 0


def get_report_owner(filename: str) -> str | None:
    row = reports_repo.get_by_filename(filename)
    if row is not None:
        return row.get("created_by")
    sidecar = _read_sidecar(OUTPUT_DIR / filename)
    if sidecar:
        return sidecar.get("created_by")
    return None


def all_jobs_sorted() -> list[dict]:
    return all_jobs_sorted_for(user_id=None, show_all=True)