"""
gunicorn.conf.py — production server config.

On Render, the platform assigns a PORT and expects the service to bind to it.
Locally, we default to 8000.
"""
import multiprocessing
import os


# Render sets PORT. Locally, fall back to 8000.
port = os.environ.get("PORT", "8000")
bind = f"0.0.0.0:{port}"

workers = max(2, multiprocessing.cpu_count())
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