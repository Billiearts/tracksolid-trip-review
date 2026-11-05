"""
services/prefetch.py — alarm prefetch for preview jobs.

Originally lived inside `app.py` as `_prefetch_alarms_for_preview_lazy`.
Moved here so `services/job_worker.py` can import it without a circular
dependency on `app.py` (which imports `job_worker`). On Render with
gunicorn's `post_fork` calling `job_worker.start()` before `app.py`
finishes importing, the old `from app import …` raised
`ImportError: cannot import name ... from partially initialized module`.

The public name is `prefetch_alarms_for_preview_lazy` (no leading
underscore). `app.py` keeps the underscore alias for backward compat
with anything else that may still reference it.

Memory note
-----------
`MAX_PREFETCH_IMEIS = 20` is deliberate. A 500-truck preview × 3-month
alarms ≈ 1 GB — that's what OOM'd the 512 MB Render container. Beyond
20 IMEIs each trip fetches its own alarms (slower per trip, but bounded
memory).
"""
from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

from jimi import RateLimitExceeded, fetch_alarms_chunked
from report_analysis import YARD_LOOKBACK, history_window
from services.ratelimit import is_rate_limited


log = logging.getLogger(__name__)


MAX_PREFETCH_IMEIS = 20


def prefetch_alarms_for_preview_lazy(trips, include_history, *,
                                     max_workers: int = 4):
    """Fetch the union of search-window and history-window alarms once
    per IMEI.

    Returns `(alarms_by_imei, bounds_by_imei)`:
      alarms_by_imei : {imei: [alarm dict, ...]}
      bounds_by_imei : {imei: (begin_dt, end_dt)}  — the window those
                       alarms cover. `compute_report` uses these to
                       decide whether its prefetch is reusable.

    Returns `({}, {})` (empty dicts) when:
      - `trips` is empty, or
      - there are more than `MAX_PREFETCH_IMEIS` unique trucks.

    In both cases each trip fetches its own alarms inside
    `compute_report()`, which is the safe bounded-memory path.

    Raises `RateLimitExceeded` if the global quota short-circuit fires
    before or during the fetch.
    """
    if not trips:
        return {}, {}

    per_imei_bounds: dict[str, tuple[datetime, datetime]] = {}
    for t in trips:
        imei = t["imei"]
        t_start = datetime.strptime(
            t["start"], "%Y-%m-%d %H:%M:%S"
        ).replace(tzinfo=timezone.utc)
        t_end = datetime.strptime(
            t["end"], "%Y-%m-%d %H:%M:%S"
        ).replace(tzinfo=timezone.utc)
        if imei not in per_imei_bounds:
            per_imei_bounds[imei] = (t_start, t_end)
        else:
            b, e = per_imei_bounds[imei]
            per_imei_bounds[imei] = (min(b, t_start), max(e, t_end))

    if len(per_imei_bounds) > MAX_PREFETCH_IMEIS:
        log.info(
            "preview: %d unique trucks exceeds prefetch cap (%d) — "
            "each trip will fetch its own alarms",
            len(per_imei_bounds), MAX_PREFETCH_IMEIS,
        )
        return {}, {}

    expanded: dict[str, tuple[datetime, datetime]] = {}
    for imei, (b, e) in per_imei_bounds.items():
        if include_history:
            hist_begin, hist_end = history_window(b)
            expanded[imei] = (min(b, hist_begin), max(e, hist_end))
        else:
            expanded[imei] = (b - YARD_LOOKBACK, e)

    def _fetch_one(imei):
        b, e = expanded[imei]
        try:
            return imei, fetch_alarms_chunked(imei, b, e)
        except RateLimitExceeded:
            raise
        except Exception as exc:
            log.warning(
                "preview: alarm prefetch failed for %s: %s", imei, exc
            )
            return imei, []

    if is_rate_limited():
        raise RateLimitExceeded("rate limited (before alarm prefetch)")

    with ThreadPoolExecutor(
        max_workers=max_workers,
        thread_name_prefix="preview-alarms",
    ) as pool:
        results = list(pool.map(_fetch_one, list(expanded.keys())))

    return dict(results), expanded