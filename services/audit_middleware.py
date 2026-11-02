"""
services/audit_middleware.py — Flask request/response hooks for audit.

What it does:

  before_request
    - Installs a per-request correlation id (`g.audit_request_id`).
    - Records the request start time for the duration calculation.

  after_request
    - Writes one `request.end` audit row *only when the request is
      interesting*:
        * status code >= 400 (4xx/5xx)
        * duration >= SLOW_REQUEST_MS (default 5000 ms)
        * the request already wrote >= MIN_ROWS_FOR_END_ROW audit rows
          (so the end row correlates them)
    - Always records the request_id in `g` so callers that wrote their
      own rows can pass it through.

  teardown_request
    - Ensures the end row is written even if the response was never
      returned (e.g. an uncaught exception mid-request).

The middleware does NOT write a row for every request. On a healthy
system almost every request is 200 OK in <5 s and gets no end row, so
the audit_log stays focused on state changes. Slow or failing requests
are captured because they're the ones you'll want to look at later.

Config (env vars):
  AUDIT_SLOW_REQUEST_MS   default 5000
  AUDIT_ENABLE_END_ROWS   default "1" (set "0" to disable entirely)
"""
from __future__ import annotations

import logging
import os
import time

from flask import g, request, session

from services import audit


log = logging.getLogger(__name__)


_SLOW_MS = int(os.environ.get("AUDIT_SLOW_REQUEST_MS", "5000"))
_ENABLE_END_ROWS = os.environ.get("AUDIT_ENABLE_END_ROWS", "1") != "0"


# Events that never produce an end row even when slow, because they ARE
# the end row (avoid double-counting the audit trail itself).
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
    # Anything under the audit admin UI is already audited by the route.
    return endpoint.startswith("admin.audit")


def _row_count_for_request(request_id: str | None) -> int:
    """Count rows already written with this request_id.

    We don't want to spend a query per request, so this is only called
    when the request is already interesting (slow or error). It's fine
    for it to be slightly stale.
    """
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
        endpoint = (request.endpoint or "") if request.has_request_context() else ""

        # Decide whether this request deserves an end row.
        interesting = (
            status_code >= 400
            or duration_ms >= _SLOW_MS
        )
        if not interesting:
            return
        if _is_skip_endpoint(endpoint):
            return

        row_count = _row_count_for_request(request_id)
        if row_count == 0 and status_code < 400 and duration_ms < _SLOW_MS:
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

        detail_bits = [
            f"{request.method} {request.path}",
            f"→ {status_code}",
            f"in {duration_ms} ms",
        ]
        if row_count:
            detail_bits.append(f"({row_count} related audit row(s))")
        if error:
            detail_bits.append(f"error: {error[:300]}")

        audit.record(
            "request.end",
            severity=severity,
            outcome=outcome,
            target=request.endpoint,
            detail=" ".join(detail_bits),
            meta={
                "query_keys":    list(request.args.keys())[:20],
                "form_keys":     list(request.form.keys())[:20],
                "content_type":  (request.content_type or "")[:80],
                "content_length": request.content_length,
                "referer":       (request.referrer or "")[:300],
            },
            status_code=status_code,
            duration_ms=duration_ms,
            request_id=request_id,
            tags=["request"] + ([f"http_{status_code}"] if status_code >= 400 else []),
        )
    except Exception as exc:
        log.warning("audit_middleware: could not write end row: %s", exc)


def register_audit_middleware(app) -> None:
    """Wire the middleware into a Flask app."""

    if not _ENABLE_END_ROWS:
        log.info("audit_middleware: end rows disabled (AUDIT_ENABLE_END_ROWS=0)")
        return

    @app.before_request
    def _audit_before():
        try:
            g.audit_request_id = audit.begin_request()
            g.audit_started_at = time.monotonic()
        except Exception:
            # If we can't even set up the request id, don't break the app.
            pass

    @app.after_request
    def _audit_after(response):
        try:
            started = getattr(g, "audit_started_at", None)
            duration_ms = (
                int((time.monotonic() - started) * 1000)
                if started is not None else 0
            )
            # Stash the timing on the response so downstream tooling can
            # read it without recomputing.
            if hasattr(response, "headers"):
                response.headers["X-Request-Duration-Ms"] = str(duration_ms)
            _write_end_row(status_code=response.status_code,
                           duration_ms=duration_ms)
        except Exception as exc:
            log.warning("audit_middleware: after_request failed: %s", exc)
        return response

    @app.teardown_request
    def _audit_teardown(exc):
        """If the view raised, after_request never runs. Write an end row
        here for 500s so the failure lands in the audit trail."""
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