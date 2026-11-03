"""
rebuild_audit_chain.py — one-off repair for a forked audit chain.

Walks every row of `audit_log` in `chain_seq` order (monotonic), recomputes
`prev_hash` and `hash` for each row from the canonical genesis, and updates
the chain head.

Run once against the real database, from the project root:

    python rebuild_audit_chain.py
"""
from __future__ import annotations

import sys
from datetime import datetime, timezone

from services.supabase_client import admin_client
from services.audit import _row_digest, _client


GENESIS = "0" * 64
PAGE = 500


def _diagnose(client) -> dict:
    forks = (client.table("audit_log")
             .select("prev_hash")
             .not_.is_("prev_hash", "null")
             .execute().data or [])
    from collections import Counter
    counts = Counter(r["prev_hash"] for r in forks if r.get("prev_hash"))
    dup = {h: n for h, n in counts.items() if n > 1}

    resp = client.table("audit_log").select("id", count="exact").execute()
    total = int(getattr(resp, "count", 0) or 0)

    return {
        "total_rows":     total,
        "fork_heads":     len(dup),
        "fork_instances": sum(n - 1 for n in dup.values()),
    }


def main() -> int:
    client = _client()

    print("=== Before rebuild ===")
    before = _diagnose(client)
    print(f"  total rows:      {before['total_rows']}")
    print(f"  fork points:     {before['fork_heads']}")
    print(f"  extra fork rows: {before['fork_instances']}")

    print("\n=== Rebuilding chain ===")
    prev = GENESIS
    page = 0
    total = 0

    while True:
        resp = (client.table("audit_log")
                .select("id,event,actor_id,actor_email,target,detail,"
                        "severity,outcome,status_code,duration_ms,"
                        "request_id,meta,diff_json,tags,ip,created_at")
                .order("chain_seq", desc=False)
                .range(page * PAGE, page * PAGE + PAGE - 1)
                .execute())
        rows = resp.data or []
        if not rows:
            break

        for r in rows:
            new_hash = _row_digest(r, prev)
            (client.table("audit_log")
             .update({"prev_hash": prev, "hash": new_hash})
             .eq("id", r["id"])
             .execute())
            prev = new_hash
            total += 1

        page += 1
        print(f"  ... {total} rows rewritten")

    # Update the chain head to the last row's hash.
    (client.table("audit_chain_head").update({
        "last_hash":  prev,
        "last_id":    None,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }).eq("id", 1).execute())

    print(f"\n=== Done. {total} rows rewritten. ===")
    print(f"  new chain head: {prev}")

    print("\n=== After rebuild ===")
    after = _diagnose(client)
    print(f"  total rows:      {after['total_rows']}")
    print(f"  fork points:     {after['fork_heads']}")
    print(f"  extra fork rows: {after['fork_instances']}")

    if after["fork_heads"] == 0 and after["fork_instances"] == 0:
        print("\nOK — chain is clean. You can now create the unique index.")
        return 0
    print("\nWARN — forks still present. Do NOT create the unique index yet.")
    return 1


if __name__ == "__main__":
    sys.exit(main())