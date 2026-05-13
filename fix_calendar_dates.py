"""
One-off migration: fixes calendar_date day/month swap in tracker.db.

Background
----------
Between 2026-05-08 and 2026-05-09, Airbnb changed their calendar testid format
from DD/MM/YYYY to MM/DD/YYYY (under en-US locale). tracker.py's parser kept
assuming DD/MM, so on the affected scrapes every stored calendar_date had its
month and day swapped, and lexicographic window filtering caused many rows
to be dropped — only ~15-19 sparse rows survived per check_date.

The parser was corrected on 2026-05-13 to use MM/DD; the fixed run that day
overwrote any buggy same-day rows via INSERT OR REPLACE.

Affected check_dates: 2026-05-09 and 2026-05-11.

This script
-----------
  1. Backs up tracker.db to tracker.db.backup-<timestamp>
  2. For rows in the affected check_dates, swaps the MM and DD components of
     calendar_date in place.

Usage:
    python fix_calendar_dates.py --dry-run    # preview only
    python fix_calendar_dates.py              # apply
"""
import argparse
import shutil
import sqlite3
import sys
from datetime import date, datetime
from pathlib import Path

DB_PATH = Path(__file__).parent / "tracker.db"
DEFAULT_BUGGY_DATES = ["2026-05-09", "2026-05-11"]


def main():
    ap = argparse.ArgumentParser(
        description="Fix MM/DD swap in tracker.db calendar_date values.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("--db", default=str(DB_PATH),
                    help=f"SQLite database path (default: {DB_PATH})")
    ap.add_argument("--dry-run", action="store_true",
                    help="Print counts and a sample without modifying the DB")
    ap.add_argument("--check-dates", nargs="+", default=DEFAULT_BUGGY_DATES,
                    help=f"Buggy check_date values to swap (default: {' '.join(DEFAULT_BUGGY_DATES)})")
    args = ap.parse_args()

    db = Path(args.db)
    if not db.exists():
        print(f"[ERROR] {db} not found", file=sys.stderr)
        sys.exit(1)

    targets = args.check_dates
    placeholders = ",".join("?" * len(targets))

    conn = sqlite3.connect(str(db))
    cur = conn.cursor()

    cur.execute("SELECT COUNT(*) FROM snapshots")
    total = cur.fetchone()[0]
    cur.execute(
        f"SELECT COUNT(*) FROM snapshots WHERE check_date IN ({placeholders})",
        targets,
    )
    to_fix = cur.fetchone()[0]

    print(f"Database          : {db}")
    print(f"Total rows        : {total}")
    print(f"Target check_dates: {', '.join(targets)}")
    print(f"Rows to swap      : {to_fix}")

    if to_fix == 0:
        print("\nNothing to swap. Exiting.")
        conn.close()
        return

    if args.dry_run:
        cur.execute(
            f"""SELECT check_date, listing_id, calendar_date, status
                  FROM snapshots
                 WHERE check_date IN ({placeholders})
                 ORDER BY check_date, listing_id, calendar_date
                 LIMIT 20""",
            targets,
        )
        print("\n[dry-run] sample of rows (calendar_date before → after):")
        for chk, lid, cd, status in cur.fetchall():
            new_cd = f"{cd[0:4]}-{cd[8:10]}-{cd[5:7]}"
            print(f"  {chk}  {lid:25s}  {cd} → {new_cd}  ({status})")
        print("\n[dry-run] no changes committed.")
        conn.close()
        return

    # Backup before mutating
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup = db.with_name(f"tracker.db.backup-{ts}")
    shutil.copy2(db, backup)
    print(f"\nBackup written → {backup}")

    # Two-step in-place swap to avoid transient UNIQUE collisions
    # (e.g. real Jun 7 stored as "2026-06-07" and real Jul 6 stored as
    # "2026-07-06" would swap into each other's keys mid-update).
    #
    #   Step 1: prefix every target row's calendar_date with 'X'.
    #   Step 2: strip the prefix and swap MM and DD slots.
    cur.execute("BEGIN")
    cur.execute(
        f"""UPDATE snapshots
               SET calendar_date = 'X' || calendar_date
             WHERE check_date IN ({placeholders})""",
        targets,
    )
    prefixed = cur.rowcount

    cur.execute("""
        UPDATE snapshots
           SET calendar_date = substr(calendar_date, 2, 4) || '-' ||
                               substr(calendar_date, 10, 2) || '-' ||
                               substr(calendar_date, 7, 2)
         WHERE calendar_date LIKE 'X%'
    """)
    swapped = cur.rowcount
    conn.commit()

    cur.execute("SELECT COUNT(*) FROM snapshots WHERE calendar_date LIKE 'X%'")
    leftover = cur.fetchone()[0]

    print(f"Prefixed {prefixed} rows, swapped {swapped} rows.")
    if leftover:
        print(f"[WARN] {leftover} rows still have 'X' prefix.")
    else:
        print(f"\n✔  Migration complete. Backup: {backup}")

    conn.close()


if __name__ == "__main__":
    main()
