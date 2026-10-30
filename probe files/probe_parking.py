"""
probe_parking.py — quick test of the parking endpoint with different params.

Run:  python probe_parking.py
"""
from datetime import datetime, timedelta, timezone

from jimi import api_call, USER_ID


def main() -> None:
    imei = input("IMEI to test (e.g. 865135060578158): ").strip()
    if not imei:
        raise SystemExit("Need an IMEI.")

    now = datetime.now(timezone.utc).replace(microsecond=0)
    end = now
    begin = now - timedelta(days=3)

    print(f"\nUser ID:  {USER_ID}")
    print(f"IMEI:     {imei}")
    print(f"Window:   {begin:%Y-%m-%d %H:%M:%S} → {end:%Y-%m-%d %H:%M:%S}")
    print(f"acc_type: off (parking)\n")

    common = {
        "account":    USER_ID,
        "imeis":      imei,
        "start_time": begin.strftime("%Y-%m-%d %H:%M:%S"),
        "end_time":   end.strftime("%Y-%m-%d %H:%M:%S"),
        "acc_type":   "off",
    }

    variants = [
        ("start_row=1, page_size=100", "1", "100"),
        ("start_row=1, page_size=50",  "1", "50"),
        ("start_row=0, page_size=100", "0", "100"),
        ("start_row=0, page_size=50",  "0", "50"),
        ("start_row=1, page_size=10",  "1", "10"),
        ("start_row=0, page_size=10",  "0", "10"),
    ]

    winner = None
    for label, start_row, page_size in variants:
        print(f"--- trying {label} ---")
        params = {**common, "start_row": start_row, "page_size": page_size}
        try:
            resp = api_call("jimi.open.platform.report.parking", params)
        except Exception as exc:
            print(f"    FAILED: {exc}\n")
            continue

        data = resp.get("data") or {}
        rows = data.get("rows") or []
        print(f"    code: {resp.get('code')}")
        print(f"    dataTotalRows: {data.get('dataTotalRows')}")
        print(f"    rows returned: {len(rows)}")
        if rows:
            first = rows[0]
            print(f"    sample row:")
            for k in ("imei", "startTime", "endTime", "durSecond",
                      "addr", "deviceName"):
                if k in first:
                    print(f"        {k}: {first[k]}")
        winner = label
        print()
        break

    if winner is None:
        print("All variants failed. Post the output above.")
        return

    print(f"Winner: {winner}")
    print("\nUpdate fetch_parking in jimi.py if it doesn't already use these values.")


if __name__ == "__main__":
    main()