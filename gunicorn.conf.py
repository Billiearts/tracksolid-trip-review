"""
gunicorn.conf.py — production server config.

Workers = 1 on Render's free tier. gthread (threads) provides concurrency
for I/O-bound work. Render sets WEB_CONCURRENCY=1 for 1-CPU instances.
"""
import os


port = os.environ.get("PORT", "8000")
bind = f"0.0.0.0:{port}"

workers = int(os.environ.get("WEB_CONCURRENCY", "1"))
worker_class = "gthread"
threads = 4

timeout = 180
graceful_timeout = 30
keepalive = 5

max_requests = 500
max_requests_jitter = 50

accesslog = "-"
errorlog = "-"
loglevel = os.environ.get("GUNICORN_LOGLEVEL", "debug")

forwarded_allow_ips = "*"


def on_starting(server):
    """Runs once in the master. Keep it fast and side-effect-light: the
    master should not import the full app or run maintenance loops (that
    happens in post_fork, inside the worker)."""
    try:
        from services import jobs
        from services import sessions as sessions_svc

        swept = jobs.sweep_stale()
        if swept:
            server.log.info("swept %d stale report job(s) on startup", swept)

        purged = sessions_svc.purge_expired()
        if purged:
            server.log.info("purged %d expired session(s) on startup", purged)
    except Exception as exc:
        server.log.warning("startup maintenance failed: %s", exc)


def post_fork(server, worker):
    """Runs in each worker after the fork. Import wsgi:app here so a broken
    import crashes loudly with a traceback instead of silently hanging."""
    try:
        server.log.info("worker %s: forked", worker.pid)
    except Exception:
        pass

    try:
        from wsgi import app  # noqa: F401
        server.log.info("worker %s: imported wsgi:app OK", worker.pid)
    except Exception as exc:
        try:
            server.log.error("worker %s: import wsgi:app FAILED: %s",
                             worker.pid, exc, exc_info=True)
        except Exception:
            pass
        raise

    # Per-worker audit maintenance thread.
    #
    # Prune/verify timestamps are persisted in audit_chain_head.meta so a
    # worker recycle (Render restarts the worker every max_requests
    # requests) does NOT re-run maintenance on every boot. Without this,
    # a worker that restarts every ~12 minutes calls verify_chain() every
    # 12 minutes, and because verify_chain() writes an audit row on
    # failure, it re-forks the chain on every restart — which is exactly
    # the "chain broken at <row>" loop we saw in production.
    try:
        import threading
        import time

        def _audit_maintenance_loop():
            try:
                from services import audit
                from services.supabase_client import admin_client
            except Exception as exc:
                server.log.error("audit maintenance: import failed: %s", exc)
                return

            PRUNE_INTERVAL  = 24 * 3600
            VERIFY_INTERVAL = 3600

            def _read_meta(key: str, default: float = 0.0) -> float:
                try:
                    rows = (admin_client().table("audit_chain_head")
                            .select("meta").eq("id", 1).limit(1)
                            .execute().data or [])
                    if not rows:
                        return default
                    meta = rows[0].get("meta") or {}
                    return float(meta.get(key) or default)
                except Exception:
                    return default

            def _write_meta(key: str) -> None:
                try:
                    rows = (admin_client().table("audit_chain_head")
                            .select("meta").eq("id", 1).limit(1)
                            .execute().data or [])
                    meta = dict(rows[0].get("meta") or {}) if rows else {}
                    meta[key] = time.time()
                    admin_client().table("audit_chain_head").update({
                        "meta": meta,
                    }).eq("id", 1).execute()
                except Exception as exc:
                    server.log.debug(
                        "audit maintenance: could not persist %s: %s",
                        key, exc,
                    )

            last_prune  = _read_meta("last_prune")  or 0.0
            last_verify = _read_meta("last_verify") or 0.0

            while True:
                try:
                    now = time.time()
                except Exception:
                    now = 0.0

                try:
                    if now - last_prune >= PRUNE_INTERVAL:
                        deleted = audit.prune()
                        server.log.info("audit prune: %s", deleted)
                        _write_meta("last_prune")
                        last_prune = now
                except Exception as exc:
                    server.log.warning("audit prune failed: %s", exc)

                try:
                    if now - last_verify >= VERIFY_INTERVAL:
                        result = audit.verify_chain(limit=20000)
                        if result.get("ok"):
                            server.log.info(
                                "audit chain OK (%d rows checked)",
                                result.get("checked", 0),
                            )
                        else:
                            server.log.error(
                                "audit chain BROKEN at %s",
                                result.get("first_mismatch"),
                            )
                        _write_meta("last_verify")
                        last_verify = now
                except Exception as exc:
                    server.log.warning("audit verify failed: %s", exc)

                time.sleep(300)

        t = threading.Thread(
            target=_audit_maintenance_loop,
            name=f"audit-maintenance-{worker.pid}",
            daemon=True,
        )
        t.start()
        server.log.info("worker %s: audit maintenance thread started", worker.pid)
    except Exception as exc:
        server.log.warning("worker %s: could not start audit maintenance: %s",
                           worker.pid, exc)