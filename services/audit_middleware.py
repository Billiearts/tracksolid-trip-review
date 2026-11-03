"""
services/audit_middleware.py — Flask request/response hooks for audit.

The middleware writes one `request.end` row per request *only when the
request is interesting*: status >= 400, or duration >= AUDIT_SLOW_REQUEST_MS
(default 5000 ms). Healthy fast requests produce no end row.
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


_SKIP_ENDPOINTS = frozenset({
    "static",
    "favicon",
    "healthz",
})


def _is_skip_endpoint(endpoint: str | None) -> bool:
    if not endpoint:
        return False
    if endpoint in _SKIP_ENDPOINTS:
        return True
    return endpoint.startswith("admin.audit")


def _row_count_for_request(request_id: str | None) -> int:
    if not request_id:
        return 0
    try:
        return len(audit.correlation_rows(request_id))
    except Exception:
        return 0


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

        row_count = _row_count_for_request(request_id)

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
        if row_count:
            bits.append(f"({row_count} related audit row(s))")
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