"""
services/supabase_client.py — Supabase client factory with singletons.

create_client() builds a full HTTP client and handshake; doing it on every
call is expensive. These singletons are created once per process.
"""
from __future__ import annotations

from threading import Lock

from supabase import create_client, Client

from config import (
    SUPABASE_URL, SUPABASE_ANON_KEY, SUPABASE_SERVICE_ROLE_KEY,
)


_anon_client:  Client | None = None
_admin_client: Client | None = None
_lock = Lock()


def anon_client() -> Client:
    global _anon_client
    if _anon_client is None:
        with _lock:
            if _anon_client is None:
                _anon_client = create_client(SUPABASE_URL, SUPABASE_ANON_KEY)
    return _anon_client


def admin_client() -> Client:
    global _admin_client
    if _admin_client is None:
        with _lock:
            if _admin_client is None:
                _admin_client = create_client(
                    SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY
                )
    return _admin_client