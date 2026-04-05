"""
Airbnb Booking-Detection Report
---------------------------------
Reads tracker.db (built by daily runs of tracker.py) and detects
which calendar dates transitioned from 'available' → 'unavailable',
indicating that a booking was made.

Usage:
    python report.py
    python report.py --from 2026-03-01 --to 2026-03-28
    python report.py --listing 41386386
    python report.py --csv bookings_report.csv
"""

import argparse
import csv
import sqlite3
from datetime import date, timedelta
from pathlib import Path

DB_PATH    = Path(__file__).parent / "tracker.db"
OUTPUT_CSV = Path(__file__).parent / "bookings_report.csv"


# ── Analysis ──────────────────────────────────────────────────────────────────

def detect_bookings(conn: sqlite3.Connection,
                    listing_id: str | None = None,
                    from_date: str | None  = None,
                    to_date:   str | None  = None) -> list[dict]:
    """
    For each (listing_id, calendar_date) pair, find dates that first
    appeared as 'available' and later became 'unavailable'.
    Returns one row per detected booking transition.
    """
    # Build WHERE clause
    conditions = []
    params: list = []
    if listing_id:
        conditions.append("listing_id = ?")
        params.append(listing_id)
    if from_date:
        conditions.append("calendar_date >= ?")
        params.append(from_date)
    if to_date:
        conditions.append("calendar_date <= ?")
        params.append(to_date)

    where = ("WHERE " + " AND ".join(conditions)) if conditions else ""

    # Fetch all snapshots ordered chronologically
    rows = conn.execute(f"""
        SELECT check_date, listing_id, calendar_date, status
        FROM   snapshots
        {where}
        ORDER  BY listing_id, calendar_date, check_date
    """, params).fetchall()

    if not rows:
        return []

    # Group by (listing_id, calendar_date)
    from collections import defaultdict
    groups: dict[tuple, list] = defaultdict(list)
    for check_date, lid, cal_date, status in rows:
        groups[(lid, cal_date)].append((check_date, status))

    bookings: list[dict] = []

    for (lid, cal_date), timeline in groups.items():
        # Skip any rows with a malformed date (defensive guard against bad scrapes)
        try:
            date.fromisoformat(cal_date)
        except ValueError:
            continue

        # Find the first check_date where status was 'available'
        first_avail = next((cd for cd, st in timeline if st == "available"), None)

        if first_avail is None:
            # Was blocked from the very first snapshot — pre-existing block
            continue

        # Find the first check_date AFTER first_avail where status became 'unavailable'
        became_unavail = next(
            (cd for cd, st in timeline if cd > first_avail and st == "unavailable"),
            None,
        )

        if became_unavail is None:
            # Still available in all snapshots — no booking detected
            continue

        bookings.append({
            "listing_id":        lid,
            "calendar_date":     cal_date,
            "first_seen_avail":  first_avail,
            "booking_detected":  became_unavail,
            "days_before_checkin": (
                date.fromisoformat(cal_date) -
                date.fromisoformat(became_unavail)
            ).days,
        })

    return sorted(bookings, key=lambda r: (r["listing_id"], r["calendar_date"]))


def summarise(bookings: list[dict]) -> dict[str, dict]:
    """Group booking events by listing_id and compute per-listing stats."""
    from collections import defaultdict
    by_listing: dict[str, list] = defaultdict(list)
    for b in bookings:
        by_listing[b["listing_id"]].append(b)

    summary = {}
    for lid, blist in by_listing.items():
        lead_times = [b["days_before_checkin"] for b in blist if b["days_before_checkin"] >= 0]
        summary[lid] = {
            "listing_id":         lid,
            "bookings_detected":  len(blist),
            "days_booked":        len(blist),           # 1 booking event = 1 night
            "avg_lead_days":      round(sum(lead_times) / len(lead_times), 1)
                                  if lead_times else "N/A",
            "earliest_booking":   min(b["booking_detected"] for b in blist),
            "latest_booking":     max(b["booking_detected"] for b in blist),
        }
    return summary


def get_tracking_period(conn: sqlite3.Connection,
                        listing_id: str | None = None) -> tuple[str | None, str | None]:
    """Return (first_check_date, last_check_date) in the database."""
    where  = "WHERE listing_id = ?" if listing_id else ""
    params = [listing_id] if listing_id else []
    row = conn.execute(
        f"SELECT MIN(check_date), MAX(check_date) FROM snapshots {where}", params
    ).fetchone()
    return row if row else (None, None)


def get_snapshot_count(conn: sqlite3.Connection) -> int:
    return conn.execute("SELECT COUNT(DISTINCT check_date) FROM snapshots").fetchone()[0]


# ── Output ────────────────────────────────────────────────────────────────────

def print_report(bookings: list[dict],
                 summary:  dict[str, dict],
                 from_d: str | None,
                 to_d:   str | None,
                 snap_count: int) -> None:

    print(f"\nAirbnb Booking-Detection Report")
    print(f"{'='*60}")
    if from_d or to_d:
        print(f"Calendar window : {from_d or 'start'} → {to_d or 'end'}")
    print(f"Snapshots in DB : {snap_count} daily runs")
    print()

    if not bookings:
        print("  No new bookings detected in the tracked period.")
        print("  (Either no listings have been booked, or the tracker")
        print("   hasn't run long enough to observe transitions.)\n")
        return

    # Per-listing summary
    print("── Per-listing summary ──────────────────────────────────")
    for lid, s in summary.items():
        print(f"\n  Listing  : {lid}")
        print(f"  Bookings : {s['bookings_detected']} nights detected")
        print(f"  Avg lead : {s['avg_lead_days']} days before check-in")
        print(f"  Detected : {s['earliest_booking']}  →  {s['latest_booking']}")

    # Detailed booking events
    print("\n── Detailed booking events ──────────────────────────────")
    print(f"  {'Listing':<22}  {'Cal. date':<12}  {'First avail':<12}  "
          f"{'Booked on':<12}  {'Lead days':>9}")
    print(f"  {'-'*22}  {'-'*12}  {'-'*12}  {'-'*12}  {'-'*9}")
    for b in bookings:
        print(f"  {b['listing_id']:<22}  {b['calendar_date']:<12}  "
              f"{b['first_seen_avail']:<12}  {b['booking_detected']:<12}  "
              f"{b['days_before_checkin']:>9}")
    print()


def save_csv(bookings: list[dict], path: Path) -> None:
    if not bookings:
        return
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(bookings[0].keys()))
        w.writeheader()
        w.writerows(bookings)
    print(f"✔  Detailed CSV saved → {path}")


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser(description="Airbnb Booking-Detection Report")
    ap.add_argument("--from",    dest="from_date", default=None,
                    help="Start calendar date filter  YYYY-MM-DD")
    ap.add_argument("--to",      dest="to_date",   default=None,
                    help="End calendar date filter    YYYY-MM-DD")
    ap.add_argument("--listing", "-l", default=None,
                    help="Filter to a single listing ID")
    ap.add_argument("--csv",     default=str(OUTPUT_CSV),
                    help=f"Output CSV path (default: {OUTPUT_CSV})")
    ap.add_argument("--db",      default=str(DB_PATH),
                    help=f"SQLite DB path (default: {DB_PATH})")
    args = ap.parse_args()

    db_path = Path(args.db)
    if not db_path.exists():
        print(f"[ERROR] Database not found: {db_path}")
        print("        Run  python tracker.py  first to collect snapshots.")
        raise SystemExit(1)

    conn = sqlite3.connect(str(db_path))

    snap_count             = get_snapshot_count(conn)
    first_snap, last_snap  = get_tracking_period(conn, args.listing)

    print(f"\nDatabase      : {db_path.resolve()}")
    print(f"Tracking from : {first_snap}  to  {last_snap}")
    print(f"Snapshots     : {snap_count} daily runs")

    bookings = detect_bookings(
        conn,
        listing_id=args.listing,
        from_date=args.from_date,
        to_date=args.to_date,
    )
    summary  = summarise(bookings)

    print_report(bookings, summary,
                 args.from_date, args.to_date, snap_count)

    if bookings:
        save_csv(bookings, Path(args.csv))

    conn.close()


if __name__ == "__main__":
    main()
