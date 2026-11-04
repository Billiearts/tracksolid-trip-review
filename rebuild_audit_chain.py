"""
rebuild_audit_chain.py — one-off repair for a forked audit chain.

Walks every row of `audit_log` in `chain_seq` order (monotonic), recomputes
`prev_hash` and `hash` for each row from the canonical genesis, and updates
the informational `audit_chain_head` table. The chain head that `record()`
actually reads is derived from `audit_log` itself, so this script's job is
purely to rewrite the rows so that the unique index on `prev_hash` can be
re-created cleanly and `verify_chain()` returns OK.

Also deletes any stale `audit.chain_mismatch` rows from the old
`verify_chain()` behaviour (the writer that recorded them on every
maintenance pass). Without that cleanup, the rebuild succeeds but the very
next `verify_chain()` walk sees those rows and re-reports a mismatch on
the row immediately preceding the first one it deleted.

Run once against the real database, from the project root:

    python rebuild_audit_chain.py
"""
from __future__ import annotations

import sys
from datetime import datetime, timezone

from services.supabase_client import admin_client
from services.audit import _row_digest, _client, verify_chain


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


def _clear_stale_mismatch_rows(client) -> int:
    """Delete any rows the OLD verify_chain() wrote.

    The previous version of `services.audit.verify_chain` recorded an
    `audit.chain_mismatch` row every time it found a break. Those rows
    live inside the chain they were complaining about, so on the very
    next verify pass they cause a new mismatch on the row *before* them.
    The new `verify_chain` no longer writes them (only the explicit
    admin verify does), so any `audit.chain_mismatch` rows still in the
    table are stale debris that must be removed before the rebuild can
    converge.
    """
    resp = (client.table("audit_log")
            .select("id")
            .eq("event", "audit.chain_mismatch")
            .execute())
    ids = [r["id"] for r in (resp.data or [])]
    if not ids:
        return 0
    (client.table("audit_log")
     .delete()
     .in_("id", ids)
     .execute())
    return len(ids)


def main() -> int:
    client = _client()

    print("=== Clearing stale audit.chain_mismatch rows ===")
    removed = _clear_stale_mismatch_rows(client)
    print(f"  removed {removed} stale audit.chain_mismatch row(s)")

    print("\n=== Before rebuild ===")
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
            resp_upd = (client.table("audit_log")
                        .update({"prev_hash": prev, "hash": new_hash})
                        .eq("id", r["id"])
                        .execute())
            if not (resp_upd.data or []):
                print(f"  !! UPDATE failed for row {r['id']} — no rows "
                      f"affected. Check RLS / permissions on audit_log.")
                return 1
            prev = new_hash
            total += 1

        page += 1
        print(f"  ... {total} rows rewritten")

    # Update the informational chain-head table.
    try:
        (client.table("audit_chain_head").update({
            "last_hash":  prev,
            "last_id":    None,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }).eq("id", 1).execute())
    except Exception as exc:
        print(f"  (warning) audit_chain_head mirror update failed: {exc}")

    print(f"\n=== Done. {total} rows rewritten. ===")
    print(f"  new chain head: {prev}")

    print("\n=== Verifying with verify_chain() ===")
    result = verify_chain(limit=total + 1000)
    if result.get("ok"):
        print(f"  OK — {result.get('checked', 0)} rows verified.")
    else:
        print(f"  FAILED — mismatch at {result.get('first_mismatch')}: "
              f"{result.get('error') or ''}")
        return 1

    print("\n=== After rebuild ===")
    after = _diagnose(client)
    print(f"  total rows:      {after['total_rows']}")
    print(f"  fork points:     {after['fork_heads']}")
    print(f"  extra fork rows: {after['fork_instances']}")

    if after["fork_heads"] == 0 and after["fork_instances"] == 0:
        print("\nOK — chain is clean. You can now (re-)create the unique index.")
        return 0
    print("\nWARN — forks still present. Do NOT create the unique index yet.")
    return 1


if __name__ == "__main__":
    sys.exit(main())