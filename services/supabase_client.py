"""
services/supabase_client.py — Supabase client factory (thread-local).

Supabase's Python SDK (httpx under the hood) is NOT thread-safe on
Windows. Under load from the job-worker fan-out it raises:

    [WinError 10035] A non-blocking socket operation could not be
    completed immediately

which silently broke `claim_next_queued_job()` and left preview jobs
stuck in 'queued' forever (see `services/report_jobs.py`).

Both clients are now **thread-local**: each thread gets its own instance,
its own httpx connection pool, and its own authentication state. The
per-thread cost is small (the SDK pools connections per client), and the
fix makes the worker reliable under concurrency on every platform.
"""
from __future__ import annotations

import threading

from supabase import create_client, Client

from config import (
    SUPABASE_URL, SUPABASE_ANON_KEY, SUPABASE_SERVICE_ROLE_KEY,
)


_anon_local  = threading.local()
_admin_local = threading.local()


def anon_client() -> Client:
    client = getattr(_anon_local, "client", None)
    if client is None:
        client = create_client(SUPABASE_URL, SUPABASE_ANON_KEY)
        _anon_local.client = client
    return client


def admin_client() -> Client:
    client = getattr(_admin_local, "client", None)
    if client is None:
        client = create_client(SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY)
        _admin_local.client = client
    return client