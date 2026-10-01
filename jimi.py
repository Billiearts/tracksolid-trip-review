"""
jimi.py — all the Tracksolid Pro (JIMI) API logic lives here.
"""

import hashlib
import json
import threading
import time

import requests
from datetime import datetime, timezone
from pathlib import Path

from config import APP_KEY, APP_SECRET, USER_ID, USER_PWD_MD5
from services.ratelimit import note_rate_limited


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
# Token cache
# ---------------------------------------------------------------------------

_TOKEN_CACHE_FILE = Path(__file__).parent / ".token_cache.json"
_TOKEN_SAFETY_MARGIN_SECONDS = 300

_token_cache: dict = {"token": None, "expires_at": 0.0}


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
    from datetime import timedelta as _td
    all_alarms: list = []
    cursor = begin
    while cursor < end:
        chunk_end = min(cursor + _td(days=chunk_days), end)
        try:
            batch = fetch_alarms(
                imei,
                cursor.strftime("%Y-%m-%d %H:%M:%S"),
                chunk_end.strftime("%Y-%m-%d %H:%M:%S"),
            )
            all_alarms.extend(batch)
        except RateLimitExceeded:
            raise
        except Exception as exc:
            print(f"[alarms] chunk failed: {exc}")
        cursor = chunk_end + _td(seconds=1)
    return all_alarms


# ---------------------------------------------------------------------------
# Parking / idling events — jimi.open.platform.report.parking
# ---------------------------------------------------------------------------

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


def fetch_parking(imei: str, begin: datetime, end: datetime,
                  acc_type: str = "off") -> list[dict]:
    """Fetch parking (acc_type='off') or idling (acc_type='on') events.

    Tracksolid's parking endpoint is picky about `start_row` / `page_size`.
    We try the conventional values (1-based row, 100-row page) first, and
    fall back automatically if the API rejects them.
    """
    base_params = {
        "account":    USER_ID,
        "imeis":      imei,
        "start_time": begin.strftime("%Y-%m-%d %H:%M:%S"),
        "end_time":   end.strftime("%Y-%m-%d %H:%M:%S"),
        "acc_type":   acc_type,
    }

    # Try in order of likelihood.
    variants = [
        {**base_params, "start_row": "1", "page_size": "100"},
        {**base_params, "start_row": "1", "page_size": "50"},
        {**base_params, "start_row": "0", "page_size": "100"},
        {**base_params, "start_row": "0", "page_size": "50"},
        {**base_params, "start_row": "1", "page_size": "10"},
        {**base_params, "start_row": "0", "page_size": "10"},
    ]

    first_response = None
    chosen = None
    last_error: Exception | None = None

    for variant in variants:
        try:
            first_response = api_call("jimi.open.platform.report.parking", variant)
            chosen = variant
            print(f"[parking] accepted start_row={variant['start_row']} "
                  f"page_size={variant['page_size']}")
            break
        except RuntimeError as exc:
            last_error = exc
            msg = str(exc)
            # Only retry on the parameter-validation error; bubble others up.
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

    # Fetch further pages if the API says there are more rows.
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

    return [_normalise_parking_row(r) for r in all_rows]


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