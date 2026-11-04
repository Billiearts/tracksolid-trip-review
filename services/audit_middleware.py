"""
services/audit_middleware.py — Flask request/response hooks for audit.

Writes one `request.end` audit row per request *only when the request is
interesting*: status >= 400, or duration >= AUDIT_SLOW_REQUEST_MS (default
5000 ms). Healthy fast requests produce no end row.

The previous version of this module called `audit.correlation_rows()` on
every interesting request to count "related rows" for the detail string.
That SELECT * against audit_log was ~150 ms per call and dominated the
page-load latency. It is gone; the detail string no longer includes a
count. Use `/admin/audit?request_id=<uuid>` if you need the correlated
rows.

Polling endpoints (`/api/status`, `/api/jobs`, `/live/api/fleet`,
`/live/api/heartbeat`) are excluded entirely. Without this exclusion, a
slow poll (which is now itself caused by worker contention) would write an
audit row, which makes the next poll slower, which writes another row —
a feedback loop that only manifests once the worker is already saturated.
"""
from __future__ import annotations

import logging
import os
import time

from flask import g, request

from services import audit


log = logging.getLogger(__name__)


_SLOW_MS = int(os.environ.get("AUDIT_SLOW_REQUEST_MS", "5000"))
_ENABLE_END_ROWS = os.environ.get("AUDIT_ENABLE_END_ROWS", "1") != "0"


# Flask endpoint names (not URLs). Polling endpoints are excluded outright:
# they are high-frequency, low-value, and — crucially — writing an audit
# row for them adds Supabase round trips that make the *next* poll slower,
# which is exactly the wrong feedback loop on a 4-thread gthread worker.
_SKIP_ENDPOINTS = frozenset({
    "static",
    "favicon",
    "healthz",
    "api_status",                    # GET /api/status — polled per tab
    "api_jobs",                      # GET /api/jobs — polled by /reports
    "api_preview",                   # GET /api/preview/<id> — polled
    "live_fleet.api_fleet",          # GET /live/api/fleet — polled
    "live_fleet.api_heartbeat",      # POST /live/api/heartbeat — polled
})


def _is_skip_endpoint(endpoint: str | None) -> bool:
    if not endpoint:
        return False
    if endpoint in _SKIP_ENDPOINTS:
        return True
    return endpoint.startswith("admin.audit")


def _write_end_row(*, status_code: int, duration_ms: int,
                   error: str | None = None) -> None:
    try:
        request_id = audit.current_request_id()
        endpoint = request.endpoint if request else ""

        interesting = (status_code >= 400 or duration_ms >= _SLOW_MS)
        if not interesting:
            return
        if _is_skip_endpoint(endpoint):
            return

        severity = "info"
        if status_code >= 500:
            severity = "critical"
        elif status_code >= 400:
            severity = "warning"
        elif duration_ms >= _SLOW_MS:
            severity = "warning"

        outcome = "success"
        if status_code >= 400:
            outcome = "failure"
        elif error:
            outcome = "partial"

        bits = [
            f"{request.method} {request.path}",
            f"→ {status_code}",
            f"in {duration_ms} ms",
        ]
        if error:
            bits.append(f"error: {error[:300]}")

        audit.record(
            "request.end",
            severity=severity,
            outcome=outcome,
            target=endpoint,
            detail=" ".join(bits),
            meta={
                "query_keys":     list(request.args.keys())[:20],
                "form_keys":      list(request.form.keys())[:20],
                "content_type":   (request.content_type or "")[:80],
                "content_length": request.content_length,
                "referer":        (request.referrer or "")[:300],
            },
            status_code=status_code,
            duration_ms=duration_ms,
            request_id=request_id,
            tags=["request"] + ([f"http_{status_code}"] if status_code >= 400 else []),
        )
    except Exception as exc:
        log.warning("audit_middleware: could not write end row: %s", exc)


def register_audit_middleware(app) -> None:
    if not _ENABLE_END_ROWS:
        log.info("audit_middleware: end rows disabled")
        return

    @app.before_request
    def _audit_before():
        try:
            g.audit_request_id = audit.begin_request()
            g.audit_started_at = time.monotonic()
        except Exception:
            pass

    @app.after_request
    def _audit_after(response):
        try:
            started = getattr(g, "audit_started_at", None)
            duration_ms = (
                int((time.monotonic() - started) * 1000)
                if started is not None else 0
            )
            if hasattr(response, "headers"):
                response.headers["X-Request-Duration-Ms"] = str(duration_ms)
            _write_end_row(status_code=response.status_code,
                           duration_ms=duration_ms)
        except Exception as exc:
            log.warning("audit_middleware: after_request failed: %s", exc)
        return response

    @app.teardown_request
    def _audit_teardown(exc):
        try:
            if exc is None:
                return
            started = getattr(g, "audit_started_at", None)
            duration_ms = (
                int((time.monotonic() - started) * 1000)
                if started is not None else 0
            )
            _write_end_row(
                status_code=500,
                duration_ms=duration_ms,
                error=f"{type(exc).__name__}: {exc}",
            )
        except Exception:
            pass

    log.info("audit_middleware registered (slow threshold=%d ms)", _SLOW_MS)