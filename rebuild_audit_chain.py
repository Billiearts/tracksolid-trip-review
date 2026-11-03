"""
rebuild_audit_chain.py — one-off. Recompute every audit row's hash in
chain_seq order and update the chain head. Run once, then delete.

    python rebuild_audit_chain.py
"""
from services.supabase_client import admin_client
from services.audit import _row_digest, _client


def main():
    client = _client()
    prev = "0" * 64
    page = 0
    page_size = 500
    total = 0

    while True:
        resp = (client.table("audit_log")
                .select("*")
                .order("chain_seq", desc=False)
                .range(page * page_size, page * page_size + page_size - 1)
                .execute())
        rows = resp.data or []
        if not rows:
            break
        for r in rows:
            new_hash = _row_digest(r, prev)
            client.table("audit_log").update({
                "prev_hash": prev,
                "hash":      new_hash,
            }).eq("id", r["id"]).execute()
            prev = new_hash
            total += 1
        page += 1
        print(f"  rebuilt {total} rows...")

    # Final chain head
    client.table("audit_chain_head").update({
        "last_hash": prev,
        "last_id":   None,
        "updated_at": __import__("datetime").datetime.now(
            __import__("datetime").timezone.utc
        ).isoformat(),
    }).eq("id", 1).execute()

    print(f"Done. {total} rows rebuilt. New head: {prev[:16]}…")


if __name__ == "__main__":
    main()