"""
jimi.py — all the Tracksolid Pro (JIMI) API logic lives here.
"""

import hashlib
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

import requests

from config import APP_KEY, APP_SECRET, USER_ID, USER_PWD_MD5
from services.ratelimit import note_rate_limited, is_rate_limited


BASE_URL = "https://eu-open.tracksolidpro.com/route/rest"


class RateLimitExceeded(RuntimeError):
    """Raised when Tracksolid returns code 1006 (request frequency too high)."""


# ---------------------------------------------------------------------------
# Sign
# ---------------------------------------------------------------------------

def make_sign(params: dict, app_secret: str) -> str:
    clean = {
        k: v
        for k, v in params.items()
        if k != "sign" and v is not None and v != ""
    }
    joined = "".join(f"{k}{clean[k]}" for k in sorted(clean.keys()))
    to_hash = f"{app_secret}{joined}{app_secret}"
    return hashlib.md5(to_hash.encode("utf-8")).hexdigest().upper()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def utc_now_str() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def build_common_params(method: str) -> dict:
    return {
        "method":      method,
        "timestamp":   utc_now_str(),
        "app_key":     APP_KEY,
        "sign_method": "md5",
        "v":           "1.0",
        "format":      "json",
    }


# ---------------------------------------------------------------------------
# Parallel fan-out helper
# ---------------------------------------------------------------------------

# Kept low on purpose. Tracksolid's daily quota is per-account; the parallel
# fan-out in app.py already uses 4, so total concurrent calls during a
# preview should stay <= 8 to avoid hitting 1006 under load.
_HISTORY_WORKERS = 4


def parallel_call(fn, items, *, workers: int = _HISTORY_WORKERS):
    """Run `fn(item)` for each item in `items`, up to `workers` at a time.

    Returns a list in input order. Short-circuits on RateLimitExceeded:
    the exception is re-raised after the current batch finishes, so the
    caller can decide whether to abandon or fall back to sequential.

    The pre-flight `is_rate_limited()` check means a fan-out that starts
    after the quota is already exhausted (or a sibling fan-out that
    trips it) fails in milliseconds instead of firing every request and
    timing each one out.
    """
    if not items:
        return []
    if is_rate_limited():
        raise RateLimitExceeded("rate limited (short-circuit before fan-out)")

    with ThreadPoolExecutor(max_workers=workers) as pool:
        return list(pool.map(fn, items))


# ---------------------------------------------------------------------------
# Token cache
# ---------------------------------------------------------------------------

_TOKEN_CACHE_FILE = Path(__file__).parent / ".token_cache.json"
_TOKEN_SAFETY_MARGIN_SECONDS = 300

_token_cache: dict = {"token": None, "expires_at": 0.0}
_token_lock = threading.Lock()


def _load_cached_token() -> tuple[str | None, float]:
    try:
        data = json.loads(_TOKEN_CACHE_FILE.read_text(encoding="utf-8"))
        return data.get("token"), float(data.get("expires_at", 0.0))
    except (FileNotFoundError, ValueError, KeyError, TypeError):
        return None, 0.0


def _save_cached_token(token: str, expires_at: float) -> None:
    try:
        _TOKEN_CACHE_FILE.write_text(
            json.dumps({"token": token, "expires_at": expires_at}),
            encoding="utf-8",
        )
    except OSError as exc:
        print(f"[token] warning: could not persist cache ({exc})")


def _fetch_new_token() -> str:
    params = build_common_params("jimi.oauth.token.get")
    params.update({
        "user_id":      USER_ID,
        "user_pwd_md5": USER_PWD_MD5,
        "expires_in":   7200,
    })
    params["sign"] = make_sign(params, APP_SECRET)

    response = requests.post(BASE_URL, data=params, timeout=30)
    response.raise_for_status()
    payload = response.json()

    if payload.get("code") != 0:
        raise RuntimeError(f"Token fetch failed: {payload}")

    result = payload.get("result") or {}
    token = result.get("accessToken")
    expires_in = int(result.get("expiresIn", 0))
    if not token:
        raise RuntimeError(f"Token fetch returned no accessToken: {payload}")

    expires_at = time.time() + expires_in - _TOKEN_SAFETY_MARGIN_SECONDS
    _token_cache["token"] = token
    _token_cache["expires_at"] = expires_at
    _save_cached_token(token, expires_at)

    print(f"[token] fetched new token (valid ~{expires_in}s, "
          f"cached for {expires_in - _TOKEN_SAFETY_MARGIN_SECONDS}s)")
    return token


def get_access_token() -> str:
    # The token fetch itself is not thread-safe (two threads fetching
    # at the same time would each cost an API call). Lock around the
    # whole read-check-fetch path.
    with _token_lock:
        now = time.time()

        if _token_cache["token"] and now < _token_cache["expires_at"]:
            return _token_cache["token"]

        token, expires_at = _load_cached_token()
        if token and now < expires_at:
            _token_cache["token"] = token
            _token_cache["expires_at"] = expires_at
            remaining = int(expires_at - now)
            print(f"[token] using cached token from disk ({remaining}s left)")
            return token

        return _fetch_new_token()


# ---------------------------------------------------------------------------
# Generic API call
# ---------------------------------------------------------------------------

def api_call(method: str, private_params: dict | None = None) -> dict:
    params = build_common_params(method)
    params["access_token"] = get_access_token()
    if private_params:
        params.update(private_params)
    params["sign"] = make_sign(params, APP_SECRET)

    response = requests.post(BASE_URL, data=params, timeout=30)
    response.raise_for_status()
    payload = response.json()

    code = payload.get("code")
    if code == 1006:
        msg = (f"Rate limit reached on {method}: "
               f"{payload.get('message')!r}")
        note_rate_limited(msg)
        raise RateLimitExceeded(f"Tracksolid Pro rate limit reached on {method}: "
                                f"{payload.get('message')!r}")
    if code != 0:
        raise RuntimeError(
            f"API error on {method}: code={code} "
            f"message={payload.get('message')!r}"
        )
    return payload


# ---------------------------------------------------------------------------
# Alarm cache — 15 minutes, in-memory only
# ---------------------------------------------------------------------------

_alarm_cache: dict = {}
_alarm_cache_lock = threading.Lock()
_ALARM_CACHE_TTL = 900


def _cache_get(key):
    with _alarm_cache_lock:
        entry = _alarm_cache.get(key)
        if entry is None:
            return None
        expires_at, alarms = entry
        if time.time() > expires_at:
            _alarm_cache.pop(key, None)
            return None
        return alarms


def _cache_put(key, alarms):
    with _alarm_cache_lock:
        _alarm_cache[key] = (time.time() + _ALARM_CACHE_TTL, alarms)


def _cache_key(imei: str, begin_str: str, end_str: str) -> tuple:
    return (imei, begin_str, end_str)


# ---------------------------------------------------------------------------
# Alarm list helpers
# ---------------------------------------------------------------------------

def fetch_alarms(imei: str, begin_str: str, end_str: str,
                 page_size: int = 500) -> list:
    key = _cache_key(imei, begin_str, end_str)
    cached = _cache_get(key)
    if cached is not None:
        print(f"[alarms] cache hit  {begin_str} → {end_str} ({len(cached)} alarms)")
        return cached

    all_alarms: list = []
    page = 1
    while True:
        response = api_call("jimi.device.alarm.list", {
            "imeis":      imei,
            "begin_time": begin_str,
            "end_time":   end_str,
            "page":       page,
            "page_size":  page_size,
        })
        batch = response.get("result") or []
        if not isinstance(batch, list):
            batch = batch.get("list", []) if isinstance(batch, dict) else []
        if not batch:
            break

        if all_alarms and batch[0].get("alertTime") == all_alarms[0].get("alertTime"):
            break

        all_alarms.extend(batch)
        if len(batch) < page_size:
            break
        page += 1

    _cache_put(key, all_alarms)
    return all_alarms


def fetch_alarms_chunked(imei: str, begin: datetime, end: datetime,
                         chunk_days: int = 7) -> list:
    """Fetch alarms in 7-day chunks, parallelised across chunks.

    For a 3-month window this is ~13 chunks; sequentially they cost ~20s
    of wall clock, which used to dominate preview time for the first trip
    of every preview (all subsequent trips hit the alarm cache, but only
    if their ranges align exactly). Running them 4-way drops it to ~5s,
    and the per-chunk cache in `fetch_alarms` still prevents a *second*
    trip from re-fetching anything already retrieved.
    """
    from datetime import timedelta as _td

    # Build the chunk boundaries up-front so `parallel_call` can fan them
    # out evenly. The cache hit/miss decision is per-chunk, not per-range,
    # so a partial cache still saves the chunks that were already loaded.
    chunks: list[tuple[str, str]] = []
    cursor = begin
    while cursor < end:
        chunk_end = min(cursor + _td(days=chunk_days), end)
        chunks.append((
            cursor.strftime("%Y-%m-%d %H:%M:%S"),
            chunk_end.strftime("%Y-%m-%d %H:%M:%S"),
        ))
        cursor = chunk_end + _td(seconds=1)

    if not chunks:
        return []

    def _one(chunk: tuple[str, str]) -> list:
        b, e = chunk
        return fetch_alarms(imei, b, e)

    try:
        batches = parallel_call(_one, chunks, workers=4)
    except RateLimitExceeded:
        raise
    except Exception as exc:
        print(f"[alarms] parallel chunk fetch failed: {exc}")
        # Fall back to sequential — better a slow result than none.
        batches = []
        for chunk in chunks:
            try:
                batches.append(fetch_alarms(imei, chunk[0], chunk[1]))
            except RateLimitExceeded:
                raise
            except Exception as exc2:
                print(f"[alarms] chunk failed: {exc2}")

    return [a for batch in batches for a in batch]


# ---------------------------------------------------------------------------
# Parking / idling events — jimi.open.platform.report.parking
# ---------------------------------------------------------------------------

_parking_cache: dict = {}
_parking_cache_lock = threading.Lock()
_PARKING_CACHE_TTL = 900


def _parking_cache_get(key):
    with _parking_cache_lock:
        entry = _parking_cache.get(key)
        if entry is None:
            return None
        expires_at, rows = entry
        if time.time() > expires_at:
            _parking_cache.pop(key, None)
            return None
        return rows


def _parking_cache_put(key, rows):
    with _parking_cache_lock:
        _parking_cache[key] = (time.time() + _PARKING_CACHE_TTL, rows)


def _parse_parking_dt(s) -> datetime | None:
    """Parse a Tracksolid parking timestamp like '2022-12-10T01:05:33.000Z'."""
    if not s:
        return None
    s2 = str(s).replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(s2)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _normalise_parking_row(r: dict) -> dict:
    return {
        "imei":    r.get("imei"),
        "start":   _parse_parking_dt(r.get("startTime")),
        "end":     _parse_parking_dt(r.get("endTime")),
        "seconds": int(float(r.get("durSecond") or 0)),
        "address": r.get("addr") or "",
        "lat":     r.get("lat"),
        "lng":     r.get("lng"),
        "acc":     r.get("acc") or "",
    }


def _parking_rows(response: dict) -> list:
    data = response.get("data") or {}
    return data.get("rows") or []


def _parking_total(response: dict) -> int | None:
    data = response.get("data") or {}
    total = data.get("dataTotalRows")
    try:
        return int(total) if total is not None else None
    except (TypeError, ValueError):
        return None


# Module-level so we don't re-try all six start_row/page_size variants on
# every single call. The first successful variant wins; later calls start
# from it. The cache resets on process restart, which is fine — worst case
# the first call per process tries the variants again.
_parking_accepted: dict | None = None
_parking_accepted_lock = threading.Lock()


def _parking_variants(base_params: dict):
    """Try the last-known-good variant first, then the fixed fallback list."""
    global _parking_accepted
    with _parking_accepted_lock:
        cached = _parking_accepted

    if cached is not None:
        yield {**base_params, **cached}
    for variant in (
        {"start_row": "1", "page_size": "100"},
        {"start_row": "1", "page_size": "50"},
        {"start_row": "0", "page_size": "100"},
        {"start_row": "0", "page_size": "50"},
        {"start_row": "1", "page_size": "10"},
        {"start_row": "0", "page_size": "10"},
    ):
        if variant == cached:
            continue
        yield {**base_params, **variant}


def fetch_parking(imei: str, begin: datetime, end: datetime,
                  acc_type: str = "off") -> list[dict]:
    """Fetch parking (acc_type='off') or idling (acc_type='on') events.

    Cached per (imei, begin, end, acc_type) for 15 minutes. The
    `_parking_accepted` module cache means the start_row/page_size variant
    dance is paid once per process, not on every call — which alone was
    costing 3-4 wasted HTTP round trips per fetch on some accounts.
    """
    key = (imei, begin.strftime("%Y-%m-%d %H:%M:%S"),
           end.strftime("%Y-%m-%d %H:%M:%S"), acc_type)
    cached = _parking_cache_get(key)
    if cached is not None:
        print(f"[parking] cache hit {begin} → {end} ({len(cached)} rows)")
        return cached

    base_params = {
        "account":    USER_ID,
        "imeis":      imei,
        "start_time": begin.strftime("%Y-%m-%d %H:%M:%S"),
        "end_time":   end.strftime("%Y-%m-%d %H:%M:%S"),
        "acc_type":   acc_type,
    }

    first_response = None
    chosen = None
    last_error: Exception | None = None

    for variant in _parking_variants(base_params):
        try:
            first_response = api_call("jimi.open.platform.report.parking", variant)
            chosen = variant
            global _parking_accepted
            with _parking_accepted_lock:
                _parking_accepted = {
                    "start_row": variant["start_row"],
                    "page_size": variant["page_size"],
                }
            print(f"[parking] accepted start_row={variant['start_row']} "
                  f"page_size={variant['page_size']}")
            break
        except RuntimeError as exc:
            last_error = exc
            msg = str(exc)
            if "code=1001" in msg or "Parameter validation" in msg:
                continue
            raise

    if first_response is None:
        raise last_error or RuntimeError(
            "parking: every start_row/page_size variant was rejected"
        )

    all_rows = list(_parking_rows(first_response))
    total = _parking_total(first_response)
    start_row = int(chosen["start_row"])
    page_size = int(chosen["page_size"])

    page = 1
    while total is not None and len(all_rows) < total and page < 50:
        next_start = start_row + page * page_size
        try:
            resp = api_call("jimi.open.platform.report.parking", {
                **chosen, "start_row": str(next_start),
            })
        except Exception as exc:
            print(f"[parking] pagination stopped: {exc}")
            break
        more = _parking_rows(resp)
        if not more:
            break
        all_rows.extend(more)
        if len(more) < page_size:
            break
        page += 1

    rows = [_normalise_parking_row(r) for r in all_rows]
    _parking_cache_put(key, rows)
    return rows


# ---------------------------------------------------------------------------
# Legacy shim
# ---------------------------------------------------------------------------

def fetch_access_token() -> dict:
    token = get_access_token()
    return {
        "code": 0,
        "message": "success",
        "result": {"accessToken": token, "expiresIn": "7200"},
    }