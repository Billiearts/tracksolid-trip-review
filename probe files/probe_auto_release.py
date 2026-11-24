"""
Probe: exercise auto_release_finished_assignments() against a synthetic
trip whose end is already in the past. No real 24h wait required.
"""
from datetime import datetime, timedelta, timezone
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from services.supabase_client import admin_client
from services import drivers as svc

def main():
    client = admin_client()

    # 1. Pick a test driver + a truck we can safely mutate.
    driver = client.table("drivers").select("id,full_name,staff_id") \
        .is_("deleted_at", "null").limit(1).execute().data[0]
    print(f"Test driver: {driver['full_name']} ({driver['staff_id']})")

    # 2. Create an assignment ending in the past.
    now = datetime.now(timezone.utc)
    fake_imei  = "999999999999999"        # synthetic, won't hit Tracksolid
    fake_truck = "TEST-PROBE-TRUCK"
    assigned_at = now - timedelta(hours=30)

    resp = client.table("truck_assignments").insert({
        "driver_id":   driver["id"],
        "imei":        fake_imei,
        "truck_id":    fake_truck,
        "assigned_at": assigned_at.isoformat(),
        "notes":       "probe: auto_release test",
        "created_by":  None,
    }).execute()
    row = resp.data[0]
    print(f"Created assignment {row['id']} (assigned_at={assigned_at})")

    # 3. Trip end is 26 h ago — i.e. inside the assignment window.
    trip_end = now - timedelta(hours=26)

    # 4. Run the release.
    n = svc.auto_release_finished_assignments(
        [(fake_imei, trip_end)],
        reason="trip_ended",
    )
    print(f"auto_release_finished_assignments -> {n} row(s) updated")

    # 5. Verify.
    check = client.table("truck_assignments").select("*") \
        .eq("id", row["id"]).execute().data[0]
    print(f"  released_at     = {check['released_at']}")
    print(f"  released_reason = {check['released_reason']}")

    assert check["released_at"] is not None, "FAIL: not released"
    assert check["released_reason"] == "trip_ended", "FAIL: wrong reason"

    # 6. Idempotency — running again should update 0 rows.
    n2 = svc.auto_release_finished_assignments(
        [(fake_imei, trip_end)], reason="trip_ended")
    print(f"second run -> {n2} row(s) updated (expected 0)")
    assert n2 == 0, "FAIL: not idempotent"

    # 7. Clean up.
    client.table("truck_assignments").delete().eq("id", row["id"]).execute()
    print("Cleaned up. PASS.")

if __name__ == "__main__":
    main()