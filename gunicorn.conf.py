"""
gunicorn.conf.py — production server config.

Workers = 1 on Render's free tier. gthread (threads) provides concurrency
for I/O-bound work (Tracksolid, Supabase, disk) without giving up the
shared in-memory caches (sessions, previews, alarm cache).

Render sets WEB_CONCURRENCY=1 for 1-CPU instances; we honour it.
"""
import os


# Render injects PORT. Locally, fall back to 8000.
port = os.environ.get("PORT", "8000")
bind = f"0.0.0.0:{port}"

# Respect Render's hint. Default to 1 — every extra worker is a separate
# process with its own in-memory cache.
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
loglevel = os.environ.get("GUNICORN_LOGLEVEL", "info")

forwarded_allow_ips = "*"


def on_starting(server):
    """Runs once in the master process before workers fork."""
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

    # --- background audit maintenance -----------------------------------
    try:
        import threading
        import time

        def _audit_maintenance_loop():
            from services import audit
            last_prune  = 0.0
            last_verify = 0.0
            PRUNE_INTERVAL  = 24 * 3600   # once a day
            VERIFY_INTERVAL = 3600        # once an hour
            while True:
                now = time.time()
                try:
                    if now - last_prune >= PRUNE_INTERVAL:
                        deleted = audit.prune()
                        server.log.info("audit prune: %s", deleted)
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
                        last_verify = now
                except Exception as exc:
                    server.log.warning("audit verify failed: %s", exc)

                time.sleep(300)  # check every 5 minutes

        t = threading.Thread(
            target=_audit_maintenance_loop,
            name="audit-maintenance",
            daemon=True,
        )
        t.start()
        server.log.info("audit maintenance thread started")
    except Exception as exc:
        server.log.warning("could not start audit maintenance thread: %s", exc)