"""
services/storage.py — Supabase Storage wrapper for report files.

Bucket: 'reports' (private). Backend uploads on export; the download route
falls back to Storage if the local file is gone.
"""
from __future__ import annotations

import logging
from pathlib import Path

from services.supabase_client import admin_client


log = logging.getLogger(__name__)


BUCKET = "reports"
XLSX_MIME = (
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
)


def upload(local_path: Path, remote_name: str | None = None) -> str | None:
    """Upload a local file to the reports bucket.

    Returns the remote object name on success, None on failure.
    """
    if not local_path.exists():
        return None
    name = remote_name or local_path.name
    try:
        client = admin_client()
        with local_path.open("rb") as f:
            client.storage.from_(BUCKET).upload(
                name, f,
                file_options={"content-type": XLSX_MIME},
            )
        return name
    except Exception as exc:
        msg = str(exc)
        # Supabase returns a "Duplicate" / "already exists" error on re-upload.
        # Treat that as success — the file is already there.
        if "Duplicate" in msg or "already exists" in msg:
            return name
        log.warning("storage upload failed for %s: %s", name, exc)
        return None


def download_to(local_path: Path, remote_name: str) -> bool:
    """Fetch an object from Storage and write it to local_path."""
    try:
        client = admin_client()
        data = client.storage.from_(BUCKET).download(remote_name)
        if not data:
            return False
        local_path.parent.mkdir(parents=True, exist_ok=True)
        local_path.write_bytes(data)
        return True
    except Exception as exc:
        log.warning("storage download failed for %s: %s", remote_name, exc)
        return False


def signed_url(remote_name: str, expires_in: int = 300) -> str | None:
    """Short-lived signed URL for a private object (unused for now)."""
    try:
        client = admin_client()
        resp = client.storage.from_(BUCKET).create_signed_url(remote_name, expires_in)
        return resp.get("signedURL") or resp.get("signedUrl") or None
    except Exception as exc:
        log.warning("signed url failed for %s: %s", remote_name, exc)
        return None