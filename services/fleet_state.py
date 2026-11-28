"""
services/fleet_state.py — canonical "where is this truck right now"
lookup, backed by Tracksolid alarms.

Real Tracksolid alarm shape (confirmed 2026-11-28):

    {
      "alertTypeId":  "in" | "out" | "ACC_ON" | "ACC_OFF" | ...,
      "alarmTypeName": "Enter geo-fence(MAINTENANCE TIME)" | "ACC ON",
      "alertTime":    "2026-11-28 08:35:45",     # UTC
      "geoid":        "436b0137...",             # "" for non-fence
      "status":       "in" | "out",              # mirrors alertTypeId
      ...
    }

Fence names come back wrapped as "Enter geo-fence(...)" /
"Exit geo-fence(...)"; `_strip_geo_prefix` unwraps them before name
matching. The fence geoid is stable — we classify by geoid first, then
by name as a fallback.

A truck crossing two overlapping fence boundaries in a single GPS fix
fires multiple enter/exit pairs with identical `alertTime` values. The
tiebreak in `_fetch_latest_fence_event` prefers `enter` over `exit`,
and among `enter` events prefers more specific yard sub-zones over the
coarse `yard` / `trip_time` groups — so the state reflects "inside the
most specific fence the truck entered", not "whichever event happened
to sort last".

Confirmed geoid → group mapping (from a 90-day probe, 2026-10-22):
    436b0137507246a39e9a238757243c18  MAINTENANCE TIME  → maintenance
    399c56a446b744dba1fee699e2085d59  GBC AWASO         → yard
    289bc06f1ce14986a997ab67879009c4  FUELING TIME      → fueling
    442f6190169049cd8b40f648b911a6ab  WAITING TIME      → waiting
    8a8aa3c24cb34f009668c9d04bcac144  LOADED AND PARKED → loading
    0af0240624d648e98c6d4d2b283cc738  WEIGHING TIME     → weighing
    2d5fa44addce43edb77bccf771c11532  FUEL AND WAYBILL  → fueling
    1a1843ca492b40afacd816f4be83bb52  WAYBILL TIME      → waybill
    791a3bb604ef41cfa302e5dc8f2c356c  TRIP TIME GBC     → trip_time
    6c5a0541387f4fa0af8cd7d334efa857  GBC TRIP TIME     → trip_time
    03fe5d6b7f4649ed9f3fce280abd813e  GBC TAKORADI      → yard
    8dbfef0c6d214015afdaf2b868c32c2e  GBC EXITED        → on_road
    6d7102fd81924c4ebd708c3de4927334  HAULAGE TRUCKS EXIT → on_road
    2e9cc908c42e4c808b6245e9eed97760  TAKORADI ENTRY    → yard

Public API
----------
  current_state(imei)          -> dict | None
  reset_cache()                -> None
  classify_fence(name, geoid)  -> group | None

Return value
------------
current_state() returns one of:

  None                               — lookup failed (network error,
                                       rate limit swallowed, etc.);
                                       caller MUST NOT treat this as
                                       "not on a trip".

  {
    "imei":      "8651…",
    "group":     "yard" | "on_road" | "trip_time" | "maintenance"
                 | "fueling" | "loading" | "waiting" | "weighing"
                 | "waybill" | "unknown",
    "fence":     "<fence name>" | None,
    "geoid":     "<fence geoid>" | None,
    "inside":    bool,
    "at":        datetime | None,
    "raw_event": {...} | None,
  }                                  — a definitive answer. `unknown`
                                       means "fetch succeeded, but no
                                       fence event in the lookback".
"""
from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timedelta, timezone

log = logging.getLogger(__name__)

LOOKBACK = timedelta(hours=36)
STATE_TTL_SECONDS = 90

# Primary classification: geoid (stable across renames) → group.
GEOID_GROUPS = {
    # Yards
    "399c56a446b744dba1fee699e2085d59": "yard",           # GBC AWASO
    "03fe5d6b7f4649ed9f3fce280abd813e": "yard",           # GBC TAKORADI
    "2e9cc908c42e4c808b6245e9eed97760": "yard",           # TAKORADI ENTRY
    # Trip
    "791a3bb604ef41cfa302e5dc8f2c356c": "trip_time",      # TRIP TIME GBC
    "6c5a0541387f4fa0af8cd7d334efa857": "trip_time",      # GBC TRIP TIME
    # On-road / exited
    "8dbfef0c6d214015afdaf2b868c32c2e": "on_road",        # GBC EXITED
    "6d7102fd81924c4ebd708c3de4927334": "on_road",        # HAULAGE TRUCKS EXIT
    # Yard sub-zones
    "436b0137507246a39e9a238757243c18": "maintenance",    # MAINTENANCE TIME
    "289bc06f1ce14986a997ab67879009c4": "fueling",        # FUELING TIME
    "2d5fa44addce43edb77bccf771c11532": "fueling",        # FUEL AND WAYBILL
    "442f6190169049cd8b40f648b911a6ab": "waiting",        # WAITING TIME
    "8a8aa3c24cb34f009668c9d04bcac144": "loading",        # LOADED AND PARKED
    "0af0240624d648e98c6d4d2b283cc738": "weighing",       # WEIGHING TIME
    "1a1843ca492b40afacd816f4be83bb52": "waybill",        # WAYBILL TIME
}

# Fallback: if a geoid isn't in GEOID_GROUPS, match the display name.
# Longest keys are matched first so 'FUEL AND WAYBILL' beats 'FUEL'.
NAME_GROUPS = {
    "TRIP TIME": "trip_time",
    "MAINT":     "maintenance",
    "WORKSHOP":  "maintenance",
    "WEIGH":     "weighing",
    "WAYBILL":   "waybill",
    "FUEL":      "fueling",
    "WAIT":      "waiting",
    "DISPATCH":  "waiting",
    "LOAD":      "loading",
    "WASH":      "loading",
    "EXITED":    "on_road",
    "EXIT":      "on_road",
    "AWASO":     "yard",
    "TAKORADI":  "yard",
}

# Tiebreak priority among same-timestamp events. Higher = wins.
# Sub-zones beat the coarse 'yard'/'trip_time' groups because a truck
# inside MAINTENANCE TIME is also inside GBC AWASO, but the sub-zone
# is the more informative state.
SUBZONE_PRIORITY = {
    "maintenance": 4,
    "fueling":     4,
    "loading":     4,
    "waiting":     4,
    "weighing":    4,
    "waybill":     4,
    "trip_time":   2,
    "yard":        1,
}

_cache: dict[str, dict] = {}
_cache_lock = threading.Lock()

# Sentinel indicating "the fetch itself failed", distinct from None
# ("fetch succeeded, no events").
FETCH_FAILED = object()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _now() -> datetime:
    return datetime.now(timezone.utc)


def _strip_geo_prefix(name: str) -> str:
    """'Enter geo-fence(MAINTENANCE TIME)' → 'MAINTENANCE TIME'."""
    if not name:
        return ""
    s = name.strip()
    for prefix in ("Enter geo-fence(", "Exit geo-fence(",
                   "Enter geo-fence (", "Exit geo-fence ("):
        if s.lower().startswith(prefix.lower()):
            inner = s[len(prefix):]
            if inner.endswith(")"):
                inner = inner[:-1]
            return inner.strip()
    return s


def classify_fence(name: str | None,
                   geoid: str | None = None) -> str | None:
    """Map a raw fence name + geoid to one of the group values.

    Geoid wins (stable across renames); the name is a fallback.
    """
    if geoid:
        g = geoid.strip().lower()
        if g in GEOID_GROUPS:
            return GEOID_GROUPS[g]

    inner = _strip_geo_prefix(name or "").upper()
    if not inner:
        return None
    # Match the longest key first so 'FUEL AND WAYBILL' beats 'FUEL'.
    for needle in sorted(NAME_GROUPS, key=len, reverse=True):
        if needle in inner:
            return NAME_GROUPS[needle]
    return None


def _parse_ts(s) -> datetime | None:
    if s is None:
        return None
    if isinstance(s, datetime):
        return s if s.tzinfo else s.replace(tzinfo=timezone.utc)
    try:
        d = datetime.fromisoformat(str(s).replace("Z", "+00:00"))
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    except ValueError:
        pass
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(str(s), fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            pass
    return None


# ---------------------------------------------------------------------------
# Alarm field extraction — matched against real Tracksolid payloads
# (2026-11-28). The real fields are:
#
#   alertTypeId  : "in" | "out" | "ACC_ON" | "ACC_OFF" | ...
#   alarmTypeName: "Enter geo-fence(MAINTENANCE TIME)" | "ACC ON" | ...
#   geoid        : "436b0137..." (empty string for non-fence alarms)
#   alertTime    : "YYYY-MM-DD HH:MM:SS" (UTC)
#   status       : "in" | "out" (mirrors alertTypeId)
#
# ACC_ON / ACC_OFF have alertTypeId values that are NOT in/out, so
# _event_kind correctly returns None for them and they're skipped.
# ---------------------------------------------------------------------------

def _event_kind(alarm: dict) -> str | None:
    """Return 'enter' | 'exit' | None.

    Tracksolid sends the fence event kind in `alertTypeId` (values 'in'
    / 'out') and mirrors it into `status`. Older clients may send it
    in `alertId` / `eventType` / `type`, or numeric codes 24 / 25.
    """
    raw = (
        alarm.get("alertTypeId")
        or alarm.get("alert_type_id")
        or alarm.get("alertId")
        or alarm.get("alert_id")
        or alarm.get("eventType")
        or alarm.get("event_type")
        or alarm.get("type")
        or alarm.get("status")
        or ""
    )
    val = str(raw).strip().upper()
    if val in ("IN", "ENTER", "24", "1"):
        return "enter"
    if val in ("OUT", "EXIT", "25", "2"):
        return "exit"
    return None


def _fence_name(alarm: dict) -> str:
    """Fence name lives in `alarmTypeName` on the real API."""
    return (
        alarm.get("alarmTypeName")
        or alarm.get("alarm_type_name")
        or alarm.get("geofenceName")
        or alarm.get("geofence_name")
        or alarm.get("fenceName")
        or alarm.get("fence_name")
        or alarm.get("geofence")
        or ""
    )


def _fence_geoid(alarm: dict) -> str:
    return (
        alarm.get("geoid")
        or alarm.get("geoId")
        or alarm.get("geofenceId")
        or alarm.get("geofence_id")
        or ""
    )


# ---------------------------------------------------------------------------
# Core fetch
# ---------------------------------------------------------------------------

def _fetch_latest_fence_event(imei: str):
    """Return:
         - a dict      : latest fence event found
         - None        : fetch succeeded, no fence events in window
         - FETCH_FAILED: fetch failed (network / remote closed / parse)

    Tiebreaking when multiple events share the same `alertTime`:
      1. Latest timestamp wins.
      2. `enter` beats `exit` — a truck crossing two overlapping fences
         in one GPS fix fires enter/exit pairs for each; if it entered
         ANY fence in that fix, that's the state we want to reflect.
      3. Among `enter` events, more-specific sub-zones beat coarse
         groups (maintenance > trip_time > yard), because a truck in
         MAINTENANCE TIME is also inside GBC AWASO but the sub-zone is
         the more informative label.
      4. Deterministic fallback by fence name so the result is stable
         across runs.
    """
    from jimi import fetch_alarms_chunked, RateLimitExceeded

    end   = _now()
    begin = end - LOOKBACK
    try:
        alarms = fetch_alarms_chunked(imei, begin, end) or []
    except RateLimitExceeded:
        raise
    except Exception as exc:
        log.warning("fleet_state: alarms fetch failed for %s: %s",
                    imei, exc)
        return FETCH_FAILED

    candidates = []
    for a in alarms:
        kind = _event_kind(a)
        if not kind:
            continue
        name  = _fence_name(a)
        geoid = _fence_geoid(a)
        if not name and not geoid:
            continue
        when = _parse_ts(
            a.get("alertTime") or a.get("alarmTime")
            or a.get("alarm_time") or a.get("time")
            or a.get("gpsTime")
        )
        if when is None:
            continue
        candidates.append({
            "kind":  kind,
            "name":  name,
            "geoid": geoid,
            "at":    when,
            "raw":   a,
        })

    if not candidates:
        return None

    def sort_key(ev):
        grp = classify_fence(ev["name"], ev["geoid"]) or ""
        # With reverse=True on the sort call, larger tuples win.
        # So:  enter=1 > exit=0  (enter wins ties),
        #      subzone=4 > yard=1 (sub-zone wins ties),
        #      latest timestamp wins across different `at` values.
        return (
            ev["at"],
            1 if ev["kind"] == "enter" else 0,
            SUBZONE_PRIORITY.get(grp, 0),
            ev["name"] or "",
        )

    candidates.sort(key=sort_key, reverse=True)
    return candidates[0]


def _compute_state(imei: str) -> dict | None:
    try:
        ev = _fetch_latest_fence_event(imei)
    except Exception as exc:
        from jimi import RateLimitExceeded
        if isinstance(exc, RateLimitExceeded):
            raise
        log.exception("fleet_state: compute failed for %s", imei)
        return None

    if ev is FETCH_FAILED:
        # We could not determine state. Returning None tells callers
        # "we don't know" — the auto-release sweep will leave any open
        # assignment on this truck alone.
        return None

    if ev is None:
        # Fetch succeeded; truck just hasn't hit a fence in 36h.
        return {
            "imei":      imei,
            "group":     "unknown",
            "fence":     None,
            "geoid":     None,
            "inside":    False,
            "at":        None,
            "raw_event": None,
        }

    group  = classify_fence(ev["name"], ev["geoid"])
    inside = True

    if ev["kind"] == "exit":
        # Latest event is an EXIT. Where does that put us?
        if group == "yard":
            group = "on_road"
        elif group == "trip_time":
            group = "yard"     # left TRIP TIME → back in yard
        else:
            group = "unknown"  # left a workshop/fuel/loading area
        inside = False

    return {
        "imei":      imei,
        "group":     group,
        "fence":     ev["name"],
        "geoid":     ev["geoid"],
        "inside":    inside,
        "at":        ev["at"],
        "raw_event": ev["raw"],
    }


def current_state(imei: str) -> dict | None:
    """Return the cached (or fresh) state dict, or None on lookup failure.

    None means: we cannot confirm the truck's state. Callers MUST NOT
    interpret None as "the truck is back in the yard".
    """
    if not imei:
        return None

    now = time.time()
    with _cache_lock:
        cached = _cache.get(imei)
    if cached and (now - cached["_cached_at"] < STATE_TTL_SECONDS):
        return cached["state"]

    try:
        state = _compute_state(imei)
    except Exception as exc:
        from jimi import RateLimitExceeded
        if isinstance(exc, RateLimitExceeded):
            raise
        log.exception("fleet_state: current_state failed for %s", imei)
        return None

    # Do NOT cache None — we want the next call to retry.
    if state is not None:
        with _cache_lock:
            _cache[imei] = {"state": state, "_cached_at": now}
    return state


def reset_cache() -> None:
    with _cache_lock:
        _cache.clear()