"""
services/audit.py — advanced audit trail.

Public API:
    record(event, *, ...)                    -> str | None
    record_diff(event, *, before, after, ...) -> str | None
    begin_request()                          -> str
    current_request_id()                     -> str | None
    verify_chain(limit=10000)                -> dict
    export_rows(...)                         -> (mimetype, body)
    correlation_rows(request_id)             -> list[dict]
    prune(...)                               -> dict

Chain ordering:
    Rows are ordered by `chain_seq` (a Postgres sequence, monotonic). The
    hash of each row is sha256(prev_hash + canonical_json(row)). `prev_hash`
    has a UNIQUE index (`audit_log_prev_hash_unique`), so two concurrent
    writers cannot both chain off the same predecessor: one insert wins,
    the other gets a unique violation and retries after re-reading the head.
    No advisory locks, no RPCs, no external coordination.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import uuid
from datetime import datetime, timedelta, timezone

from flask import g, has_request_context, request, session

from services.supabase_client import admin_client


log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SEVERITY_LEVELS = ("debug", "info", "notice", "warning", "critical")
OUTCOME_VALUES  = ("success", "failure", "partial", "attempt")

REDACT_KEYS = (
    "password", "passwd", "pwd", "secret", "token",
    "api_key", "apikey", "authorization", "cookie", "session_id",
    "signature", "meta_signature",
)

REDACT_ALLOWLIST = frozenset({
    "access_token_present", "token_present",
})

_MAX_STRING_LEN = 2000
_MAX_META_BYTES = 32 * 1024
_MAX_DETAIL_LEN = 2000

_CHAIN_RETRY_ATTEMPTS = 3


# ---------------------------------------------------------------------------
# Per-process caches
# ---------------------------------------------------------------------------

_table_ready: bool | None = None


def _client():
    return admin_client()


def _table_ok() -> bool:
    global _table_ready
    if _table_ready is not None:
        return _table_ready
    try:
        _client().table("audit_log").select("id").limit(1).execute()
        _table_ready = True
    except Exception as exc:
        log.warning("audit_log table not ready: %s", exc)
        _table_ready = False
    return _table_ready


# ---------------------------------------------------------------------------
# Redaction
# ---------------------------------------------------------------------------

def _is_sensitive(key: str) -> bool:
    if not key:
        return False
    k = key.lower()
    if k in REDACT_ALLOWLIST:
        return False
    return any(s in k for s in REDACT_KEYS)


def _safe(value, max_len: int = _MAX_STRING_LEN):
    if value is None:
        return None
    if isinstance(value, (int, float, bool)):
        return value
    s = str(value)
    return s[:max_len]


def _redact_any(obj) -> tuple[object, bool]:
    hit = {"flag": False}

    def walk(o, depth=0):
        if depth > 12:
            return "[truncated]"
        if isinstance(o, dict):
            out = {}
            for k, v in o.items():
                if _is_sensitive(str(k)):
                    hit["flag"] = True
                    out[k] = "[redacted]"
                else:
                    out[k] = walk(v, depth + 1)
            return out
        if isinstance(o, list):
            return [walk(v, depth + 1) for v in o]
        if isinstance(o, str):
            return _safe(o, _MAX_STRING_LEN)
        if isinstance(o, (int, float, bool)) or o is None:
            return o
        return _safe(str(o), _MAX_STRING_LEN)

    redacted = walk(obj)
    return redacted, hit["flag"]


# ---------------------------------------------------------------------------
# Request context
# ---------------------------------------------------------------------------

def _ip() -> str | None:
    if not has_request_context():
        return None
    try:
        fwd = request.headers.get("X-Forwarded-For")
        if fwd:
            return fwd.split(",")[0].strip()[:64]
        return (request.remote_addr or "")[:64] or None
    except Exception:
        return None


def _ua() -> str | None:
    if not has_request_context():
        return None
    try:
        return (request.headers.get("User-Agent") or "")[:200] or None
    except Exception:
        return None


def _route() -> str | None:
    if not has_request_context():
        return None
    try:
        return (request.endpoint or "")[:120] or None
    except Exception:
        return None


def _method() -> str | None:
    if not has_request_context():
        return None
    try:
        return (request.method or "")[:8] or None
    except Exception:
        return None


def _path() -> str | None:
    if not has_request_context():
        return None
    try:
        return (request.path or "")[:300] or None
    except Exception:
        return None


def begin_request() -> str:
    rid = str(uuid.uuid4())
    if has_request_context():
        try:
            g.audit_request_id = rid
        except Exception:
            pass
    return rid


def current_request_id() -> str | None:
    if not has_request_context():
        return None
    try:
        return getattr(g, "audit_request_id", None)
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Canonicalisation for the hash chain
# ---------------------------------------------------------------------------

def _canonical_json(obj) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"),
                      default=str, ensure_ascii=False)


def _row_digest(row: dict, prev_hash: str) -> str:
    body = {
        "event":       row.get("event"),
        "actor_id":    row.get("actor_id"),
        "actor_email": row.get("actor_email"),
        "target":      row.get("target"),
        "detail":      row.get("detail"),
        "severity":    row.get("severity"),
        "outcome":     row.get("outcome"),
        "status_code": row.get("status_code"),
        "duration_ms": row.get("duration_ms"),
        "request_id":  str(row.get("request_id")) if row.get("request_id") else None,
        "meta":        row.get("meta"),
        "diff_json":   row.get("diff_json"),
        "tags":        sorted(row.get("tags") or []),
        "ip":          row.get("ip"),
        "created_at":  row.get("created_at"),
    }
    return hashlib.sha256(
        (prev_hash + _canonical_json(body)).encode("utf-8")
    ).hexdigest()


def _read_chain_head() -> str:
    try:
        resp = (_client().table("audit_chain_head")
                .select("last_hash")
                .eq("id", 1)
                .limit(1)
                .execute())
        rows = resp.data or []
        return rows[0]["last_hash"] if rows else ("0" * 64)
    except Exception:
        return "0" * 64


def _write_chain_head(new_hash: str, row_id: str | None) -> None:
    try:
        _client().table("audit_chain_head").update({
            "last_hash":  new_hash,
            "last_id":    row_id,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }).eq("id", 1).execute()
    except Exception as exc:
        log.warning("audit chain head update failed: %s", exc)


def _insert_with_chain_retry(row: dict) -> tuple[str | None, str]:
    """Insert `row` chained off the current head.

    The `audit_log_prev_hash_unique` index means two concurrent writers
    cannot both claim the same predecessor: one insert wins, the other gets
    a unique violation and retries after re-reading the head. This is the
    replacement for the old advisory-lock RPC.

    Returns (row_id, final_prev_hash).
    """
    last_exc: Exception | None = None
    for _attempt in range(_CHAIN_RETRY_ATTEMPTS):
        prev_hash = _read_chain_head()
        row["prev_hash"] = prev_hash
        row["hash"]      = _row_digest(row, prev_hash)
        try:
            resp = _client().table("audit_log").insert(row).execute()
            inserted = (resp.data or [])
            return (inserted[0]["id"] if inserted else None), prev_hash
        except Exception as exc:
            last_exc = exc
            msg = str(exc).lower()
            if ("duplicate key" in msg
                    or "unique constraint" in msg
                    or "23505" in msg):
                # Another writer claimed this head. Retry.
                continue
            raise
    if last_exc is not None:
        raise last_exc
    raise RuntimeError("audit insert: retries exhausted with no exception")


# ---------------------------------------------------------------------------
# Diff
# ---------------------------------------------------------------------------

def _compute_diff(before: dict | None, after: dict | None) -> dict | None:
    if before is None and after is None:
        return None
    before = before or {}
    after  = after  or {}
    keys = set(before) | set(after)
    out = {}
    for k in keys:
        b = before.get(k) if k in before else "<absent>"
        a = after.get(k)  if k in after  else "<absent>"
        if b != a:
            out[k] = {"before": b, "after": a}
    return out or None
# ---------------------------------------------------------------------------
# Writer
# ---------------------------------------------------------------------------

def record(
    event: str,
    *,
    severity: str = "info",
    outcome: str = "success",
    target: str | None = None,
    detail: str | None = None,
    meta: dict | None = None,
    before: dict | None = None,
    after: dict | None = None,
    tags: list[str] | None = None,
    user_id: str | None = None,
    email: str | None = None,
    status_code: int | None = None,
    duration_ms: int | None = None,
    request_id: str | None = None,
) -> str | None:
    try:
        if not _table_ok():
            return None

        severity = (severity or "info").lower()
        if severity not in SEVERITY_LEVELS:
            severity = "info"
        outcome = (outcome or "success").lower()
        if outcome not in OUTCOME_VALUES:
            outcome = "success"

        actor_id    = user_id if user_id is not None else (
            session.get("user_id") if has_request_context() else None
        )
        actor_email = email if email is not None else (
            session.get("email") if has_request_context() else None
        )

        rid = request_id if request_id is not None else current_request_id()

        meta_r, meta_hit     = _redact_any(meta or {})
        before_r, before_hit = (_redact_any(before) if before is not None
                                else (None, False))
        after_r,  after_hit  = (_redact_any(after) if after is not None
                                else (None, False))
        detail_r = _safe(detail, _MAX_DETAIL_LEN)
        if detail_r:
            for needle in REDACT_KEYS:
                if needle in detail_r.lower():
                    meta_hit = True
                    break

        try:
            if len(_canonical_json(meta_r).encode("utf-8")) > _MAX_META_BYTES:
                meta_r = {"_truncated": True,
                          "keys": list(meta_r.keys())[:20]}
                meta_hit = True
        except Exception:
            pass

        diff = _compute_diff(before_r, after_r)

        row: dict = {
            "event":       _safe(event, 64),
            "actor_id":    actor_id,
            "actor_email": _safe(actor_email, 200),
            "target":      _safe(target, 200),
            "detail":      detail_r,
            "severity":    severity,
            "outcome":     outcome,
            "status_code": status_code,
            "duration_ms": duration_ms,
            "meta":        meta_r,
            "before_json": before_r,
            "after_json":  after_r,
            "diff_json":   diff,
            "tags":        [str(t)[:40] for t in (tags or [])][:20],
            "ip":          _ip(),
            "user_agent":  _ua(),
            "request_id":  rid,
            "route":       _route(),
            "method":      _method(),
            "path":        _path(),
            "redacted":    bool(meta_hit or before_hit or after_hit),
        }
        row = {k: v for k, v in row.items() if v is not None}
        row.setdefault("meta", {})
        row["created_at"] = datetime.now(timezone.utc).isoformat()

        row_id, _prev = _insert_with_chain_retry(row)
        _write_chain_head(row["hash"], row_id)

        try:
            _evaluate_alert_rules(row, row_id)
        except Exception as exc:
            log.warning("audit alert evaluation failed: %s", exc)

        return row_id
    except Exception as exc:
        log.warning("audit.record(%s) failed: %s", event, exc)
        return None


def record_diff(event: str, *, before: dict | None, after: dict | None,
                **kwargs) -> str | None:
    kwargs.setdefault("severity", "notice")
    if "meta" not in kwargs and (before is not None or after is not None):
        kwargs["meta"] = {
            "changed_fields": list(_compute_diff(before, after) or {}),
        }
    return record(event, before=before, after=after, **kwargs)


# ---------------------------------------------------------------------------
# Alert rule engine
# ---------------------------------------------------------------------------

def _alert_rule_failed_login(row: dict):
    if row.get("event") == "auth.login_failed":
        email = (row.get("actor_email") or row.get("target") or "").lower()
        if not email:
            return None
        try:
            since = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()
            resp = (_client().table("audit_log")
                    .select("id")
                    .eq("event", "auth.login_failed")
                    .eq("actor_email", email)
                    .gte("created_at", since)
                    .limit(20)
                    .execute())
            n = len(resp.data or [])
        except Exception:
            return None
        if n >= 5:
            return ("failed_login_burst",
                    f"{n} failed logins for {email} in 10 minutes")
    return None


def _alert_rule_privilege_escalation(row: dict):
    if row.get("event") == "admin.role_change":
        old = (row.get("meta") or {}).get("old_role")
        new = (row.get("meta") or {}).get("new_role")
        if new == "admin" and old != "admin":
            return ("privilege_escalation",
                    f"{row.get('target')} was promoted to admin")
    if row.get("event") == "admin.user_update":
        detail = row.get("detail") or ""
        if "extra_roles" in detail:
            return ("extra_roles_change",
                    f"extra roles changed for {row.get('target')}: {detail[:200]}")
    return None


def _alert_rule_quota_exhaust(row: dict):
    if row.get("event") == "live.probe" and "rate limit" in (row.get("detail") or "").lower():
        return ("quota_exhaust", "Tracksolid daily quota exhausted")
    if row.get("event") == "auth.password_reset_completed" and not row.get("actor_id"):
        return ("password_reset_anonymous", "password reset completed with no actor")
    return None


def _alert_rule_fence_tamper(row: dict):
    if row.get("event") == "live.fence_save":
        meta = row.get("meta") or {}
        names = meta.get("names") or []
        if len(names) >= 6:
            return ("bulk_fence_change",
                    f"{len(names)} fences modified in one request")
    if row.get("event") == "live.fence_delete":
        return ("fence_deleted", f"fence {row.get('target')} deleted")
    return None


def _alert_rule_hash_mismatch(row: dict):
    if row.get("event") == "audit.chain_mismatch":
        return ("audit_chain_mismatch", "audit hash chain failed verification")
    return None


_ALERT_RULES = (
    _alert_rule_failed_login,
    _alert_rule_privilege_escalation,
    _alert_rule_quota_exhaust,
    _alert_rule_fence_tamper,
    _alert_rule_hash_mismatch,
)


def _evaluate_alert_rules(row: dict, row_id: str | None) -> None:
    for rule in _ALERT_RULES:
        try:
            hit = rule(row)
        except Exception as exc:
            log.debug("alert rule %s errored: %s", rule.__name__, exc)
            continue
        if not hit:
            continue
        name, message = hit
        _write_alert(
            rule=name,
            severity=("critical" if name in ("privilege_escalation",
                                             "audit_chain_mismatch")
                      else "warning"),
            message=message,
            audit_id=row_id,
            actor_id=row.get("actor_id"),
            actor_email=row.get("actor_email"),
            meta={"event": row.get("event"), "target": row.get("target")},
        )


def _write_alert(*, rule: str, severity: str, message: str,
                 audit_id: str | None = None,
                 actor_id: str | None = None,
                 actor_email: str | None = None,
                 meta: dict | None = None) -> None:
    try:
        _client().table("audit_alert").insert({
            "rule":        rule,
            "severity":    severity,
            "message":     message[:1000],
            "audit_id":    audit_id,
            "actor_id":    actor_id,
            "actor_email": actor_email,
            "meta":        meta or {},
        }).execute()
    except Exception as exc:
        log.warning("audit alert insert failed (%s): %s", rule, exc)
        # ---------------------------------------------------------------------------
# Chain verification — order by chain_seq (monotonic), not created_at
# ---------------------------------------------------------------------------

def verify_chain(limit: int = 10000) -> dict:
    try:
        page_size = 500
        prev = "0" * 64
        checked = 0
        first_mismatch = None
        first_expected = None
        first_actual   = None

        while checked < limit:
            offset = checked
            try:
                resp = (_client().table("audit_log")
                        .select("id,event,actor_id,actor_email,target,detail,"
                                "severity,outcome,status_code,duration_ms,"
                                "request_id,meta,diff_json,tags,ip,"
                                "created_at,chain_seq,hash,prev_hash")
                        .order("chain_seq", desc=False)
                        .range(offset, offset + page_size - 1)
                        .execute())
            except Exception as exc:
                return {"ok": False, "error": f"{type(exc).__name__}: {exc}",
                        "checked": checked}

            rows = resp.data or []
            if not rows:
                break

            for r in rows:
                checked += 1
                stored_hash = r.get("hash") or ""
                stored_prev = r.get("prev_hash") or ""
                if stored_prev != prev and first_mismatch is None:
                    first_mismatch = r.get("id")
                    first_expected = prev
                    first_actual   = stored_prev
                expected = _row_digest(r, prev)
                if stored_hash != expected and first_mismatch is None:
                    first_mismatch = r.get("id")
                    first_expected = expected
                    first_actual   = stored_hash
                prev = stored_hash or expected

            if len(rows) < page_size:
                break

        result = {
            "ok": first_mismatch is None,
            "checked": checked,
            "first_mismatch": first_mismatch,
            "expected": first_expected,
            "actual":   first_actual,
        }
        if first_mismatch:
            record(
                "audit.chain_mismatch",
                severity="critical",
                outcome="failure",
                detail=f"chain broken at row {first_mismatch}",
                meta={"checked": checked, "row_id": first_mismatch},
            )
        return result
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


# ---------------------------------------------------------------------------
# Read API
# ---------------------------------------------------------------------------

def _parse_ts(s) -> datetime | None:
    if not s:
        return None
    if isinstance(s, datetime):
        return s
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except ValueError:
        return None


def list_events(*, page: int = 1, per_page: int = 100,
                event: str | None = None,
                actor_email: str | None = None,
                actor_id: str | None = None,
                target: str | None = None,
                severity: str | None = None,
                outcome: str | None = None,
                request_id: str | None = None,
                days: int | None = None,
                include_debug: bool = False) -> dict:
    client = _client()
    per_page = max(10, min(500, per_page))
    offset = (max(1, page) - 1) * per_page

    q = client.table("audit_log").select("*").order("created_at", desc=True)

    if event:       q = q.eq("event", event)
    if actor_email: q = q.ilike("actor_email", f"%{actor_email}%")
    if actor_id:    q = q.eq("actor_id", actor_id)
    if target:      q = q.ilike("target", f"%{target}%")
    if severity:    q = q.eq("severity", severity)
    if outcome:     q = q.eq("outcome", outcome)
    if request_id:  q = q.eq("request_id", request_id)
    if days:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        q = q.gte("created_at", cutoff)
    if not include_debug:
        q = q.neq("severity", "debug")

    resp = q.range(offset, offset + per_page - 1).execute()
    rows = resp.data or []

    out = []
    for r in rows:
        out.append({
            "id":          r.get("id"),
            "event":       r.get("event") or "",
            "severity":    r.get("severity") or "info",
            "outcome":     r.get("outcome") or "success",
            "actor_id":    r.get("actor_id"),
            "actor_email": r.get("actor_email") or "",
            "target":      r.get("target") or "",
            "detail":      r.get("detail") or "",
            "meta":        r.get("meta") or {},
            "diff":        r.get("diff_json") or {},
            "tags":        r.get("tags") or [],
            "ip":          r.get("ip") or "",
            "user_agent":  r.get("user_agent") or "",
            "request_id":  r.get("request_id"),
            "route":       r.get("route") or "",
            "method":      r.get("method") or "",
            "path":        r.get("path") or "",
            "status_code": r.get("status_code"),
            "duration_ms": r.get("duration_ms"),
            "redacted":    bool(r.get("redacted")),
            "created_at":  _parse_ts(r.get("created_at")),
        })

    return {
        "rows":     out,
        "page":     page,
        "per_page": per_page,
        "has_more": len(rows) == per_page,
    }


def distinct_events(limit_days: int = 30) -> list[str]:
    try:
        cutoff = (datetime.now(timezone.utc)
                  - timedelta(days=limit_days)).isoformat()
        resp = (_client().table("audit_log")
                .select("event")
                .gte("created_at", cutoff)
                .limit(5000)
                .execute())
        return sorted({r.get("event") for r in (resp.data or [])
                       if r.get("event")})
    except Exception:
        return []


def distinct_severities() -> list[str]:
    return list(SEVERITY_LEVELS)


def correlation_rows(request_id: str) -> list[dict]:
    if not request_id:
        return []
    try:
        resp = (_client().table("audit_log")
                .select("*")
                .eq("request_id", request_id)
                .order("chain_seq", desc=False)
                .limit(200)
                .execute())
        out = []
        for r in (resp.data or []):
            out.append({
                "id":          r.get("id"),
                "event":       r.get("event") or "",
                "severity":    r.get("severity") or "info",
                "outcome":     r.get("outcome") or "success",
                "actor_email": r.get("actor_email") or "",
                "target":      r.get("target") or "",
                "detail":      r.get("detail") or "",
                "meta":        r.get("meta") or {},
                "diff":        r.get("diff_json") or {},
                "created_at":  _parse_ts(r.get("created_at")),
                "status_code": r.get("status_code"),
                "duration_ms": r.get("duration_ms"),
            })
        return out
    except Exception:
        return []


# ---------------------------------------------------------------------------
# Alerts
# ---------------------------------------------------------------------------

def list_alerts(*, page: int = 1, per_page: int = 50,
                only_unacked: bool = False) -> dict:
    try:
        per_page = max(10, min(500, per_page))
        offset = (max(1, page) - 1) * per_page
        q = (_client().table("audit_alert")
             .select("*")
             .order("created_at", desc=True))
        if only_unacked:
            q = q.eq("acknowledged", False)
        resp = q.range(offset, offset + per_page - 1).execute()
        rows = resp.data or []
        out = []
        for r in rows:
            out.append({
                "id":           r.get("id"),
                "rule":         r.get("rule") or "",
                "severity":     r.get("severity") or "warning",
                "message":      r.get("message") or "",
                "audit_id":     r.get("audit_id"),
                "actor_email":  r.get("actor_email") or "",
                "acknowledged": bool(r.get("acknowledged")),
                "acked_at":     _parse_ts(r.get("acked_at")),
                "meta":         r.get("meta") or {},
                "created_at":   _parse_ts(r.get("created_at")),
            })
        return {"rows": out, "page": page, "per_page": per_page,
                "has_more": len(rows) == per_page}
    except Exception as exc:
        log.warning("audit.list_alerts failed: %s", exc)
        return {"rows": [], "page": page, "per_page": per_page,
                "has_more": False}


def unacked_alert_count() -> int:
    try:
        resp = (_client().table("audit_alert")
                .select("id", count="exact")
                .eq("acknowledged", False)
                .execute())
        return int(getattr(resp, "count", 0) or 0)
    except Exception:
        return 0


def acknowledge_alert(alert_id: str, *, user_id: str | None,
                      email: str | None) -> None:
    try:
        _client().table("audit_alert").update({
            "acknowledged": True,
            "acked_by":     user_id,
            "acked_at":     datetime.now(timezone.utc).isoformat(),
        }).eq("id", alert_id).execute()
        record(
            "audit.alert_ack",
            target=alert_id,
            detail=f"acknowledged by {email or user_id}",
            severity="info",
        )
    except Exception as exc:
        log.warning("acknowledge_alert failed: %s", exc)
        # ---------------------------------------------------------------------------
# Retention / export
# ---------------------------------------------------------------------------

def prune(*, debug_days: int = 7, info_days: int = 90,
          notice_days: int = 180, warning_days: int = 365,
          critical_days: int = 3650) -> dict:
    deleted = {}
    now = datetime.now(timezone.utc)
    windows = {
        "debug":    now - timedelta(days=debug_days),
        "info":     now - timedelta(days=info_days),
        "notice":   now - timedelta(days=notice_days),
        "warning":  now - timedelta(days=warning_days),
        "critical": now - timedelta(days=critical_days),
    }
    for sev, cutoff in windows.items():
        try:
            resp = (_client().table("audit_log")
                    .delete()
                    .eq("severity", sev)
                    .lt("created_at", cutoff.isoformat())
                    .execute())
            deleted[sev] = len(resp.data or [])
        except Exception as exc:
            log.warning("audit.prune(%s) failed: %s", sev, exc)
            deleted[sev] = 0

    try:
        cutoff = (now - timedelta(days=90)).isoformat()
        resp = (_client().table("audit_alert")
                .delete()
                .eq("acknowledged", True)
                .lt("created_at", cutoff)
                .execute())
        deleted["acked_alerts"] = len(resp.data or [])
    except Exception as exc:
        log.warning("audit.prune(acked_alerts) failed: %s", exc)
        deleted["acked_alerts"] = 0

    record(
        "audit.prune",
        severity="notice",
        detail=f"pruned {sum(deleted.values())} rows",
        meta=deleted,
    )
    return deleted


def export_rows(*, fmt: str = "csv", days: int | None = None,
                max_rows: int = 100_000) -> tuple[str, str]:
    import csv
    import io

    days = days or 90
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()

    columns = [
        "created_at", "event", "severity", "outcome", "actor_email",
        "target", "detail", "request_id", "route", "method", "path",
        "status_code", "duration_ms", "ip", "tags", "hash",
    ]

    if fmt == "jsonl":
        buf = io.StringIO()
        written = 0
        while written < max_rows:
            resp = (_client().table("audit_log")
                    .select(",".join(columns) + ",meta,diff_json")
                    .gte("created_at", cutoff)
                    .order("chain_seq", desc=False)
                    .range(written, written + 499)
                    .execute())
            rows = resp.data or []
            if not rows:
                break
            for r in rows:
                buf.write(json.dumps(r, default=str) + "\n")
                written += 1
            if len(rows) < 500:
                break
        return "application/x-ndjson", buf.getvalue()

    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=columns, extrasaction="ignore")
    w.writeheader()
    written = 0
    while written < max_rows:
        resp = (_client().table("audit_log")
                .select(",".join(columns))
                .gte("created_at", cutoff)
                .order("chain_seq", desc=False)
                .range(written, written + 499)
                .execute())
        rows = resp.data or []
        if not rows:
            break
        for r in rows:
            r = dict(r)
            if isinstance(r.get("tags"), list):
                r["tags"] = "|".join(r["tags"])
            w.writerow(r)
            written += 1
        if len(rows) < 500:
            break
    return "text/csv", buf.getvalue()