# probe_fences.py
from jimi import api_call

for method in ("jimi.fence.list",
               "jimi.user.fence.list",
               "jimi.device.fence.list",
               "jimi.fence.page.list"):
    try:
        r = api_call(method, {"page": 1, "page_size": 200})
        result = r.get("result") or r.get("data")
        n = len(result) if isinstance(result, list) else (
            len((result or {}).get("list") or []) if isinstance(result, dict) else 0
        )
        print(f"{method}: OK, {n} item(s)")
        if n:
            print("  first item keys:", list(result[0].keys()) if isinstance(result, list) else list(result.get("list", [{}])[0].keys()))
    except Exception as exc:
        print(f"{method}: FAILED — {exc}")