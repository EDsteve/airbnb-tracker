"""
Airbnb Daily Snapshot Tracker
-------------------------------
Run this DAILY (via Task Scheduler or cron) to build a historical database
of each listing's calendar availability.  Results are stored in tracker.db
(SQLite) so you can later run report.py to detect bookings over time.

Usage:
    python tracker.py
    python tracker.py --months 3
"""

import argparse
import calendar
import re
import sqlite3
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path

from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

# ── Config ───────────────────────────────────────────────────────────────────
LISTINGS_FILE   = Path(__file__).parent / "listings.txt"
DB_PATH         = Path(__file__).parent / "tracker.db"
MONTHS_TO_CHECK = 3
DELAY_SECS      = 3

STEALTH_JS = """
Object.defineProperty(navigator, 'webdriver', {get: () => undefined, configurable: true});
Object.defineProperty(navigator, 'plugins',   {get: () => [1, 2, 3, 4, 5]});
Object.defineProperty(navigator, 'languages', {get: () => ['en-US', 'en']});
window.chrome = {runtime: {}, app: {isInstalled: false}};
delete window.__playwright;
delete window.__pw_manual;
"""

# ── Database ──────────────────────────────────────────────────────────────────

def init_db(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path))
    conn.execute("""
        CREATE TABLE IF NOT EXISTS snapshots (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            check_date    TEXT NOT NULL,
            listing_id    TEXT NOT NULL,
            calendar_date TEXT NOT NULL,
            status        TEXT NOT NULL,
            UNIQUE(check_date, listing_id, calendar_date)
        )
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_listing_date
            ON snapshots(listing_id, calendar_date)
    """)
    conn.commit()
    return conn


def save_snapshot(conn: sqlite3.Connection,
                  check_date: str,
                  listing_id: str,
                  availability: dict) -> int:
    """Insert today's availability snapshot; returns rows written."""
    rows = [(check_date, listing_id, cal_date, status)
            for cal_date, status in availability.items()]
    conn.executemany(
        "INSERT OR REPLACE INTO snapshots "
        "(check_date, listing_id, calendar_date, status) VALUES (?,?,?,?)",
        rows,
    )
    conn.commit()
    return len(rows)


# ── Helpers (shared with checker.py) ─────────────────────────────────────────

def extract_listing_id(raw: str):
    raw = raw.strip()
    if re.fullmatch(r"\d+", raw):
        return raw, f"https://www.airbnb.com/rooms/{raw}"
    m = re.search(r"/rooms/(\d+)", raw)
    if m:
        return m.group(1), raw.split("?")[0]
    return None, None


def load_listings(path: Path):
    entries = []
    if not path.exists():
        print(f"[WARN] {path} not found.")
        return entries
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        lid, url = extract_listing_id(line)
        if lid:
            entries.append((lid, url))
        else:
            print(f"[WARN] Cannot parse: {line!r}")
    return entries


def _parse_testid_date(tid: str) -> str | None:
    # Airbnb calendar testids use DD/MM/YYYY (e.g. calendar-day-27/03/2026)
    m = re.search(r"(\d{2}/\d{2}/\d{4})", tid)
    if m:
        dd, mm, yyyy = m.group(1).split("/")   # day first, then month
        return f"{yyyy}-{mm}-{dd}"
    m2 = re.search(r"(\d{4}-\d{2}-\d{2})", tid)
    if m2:
        return m2.group(1)
    return None


def _add_months(d: date, months: int) -> date:
    """Return the date exactly `months` calendar months after `d`."""
    month = d.month - 1 + months
    year  = d.year + month // 12
    month = month % 12 + 1
    day   = min(d.day, calendar.monthrange(year, month)[1])
    return d.replace(year=year, month=month, day=day)


def _open_calendar(page) -> bool:
    """Click the booking-widget date trigger to reopen the full popup calendar."""
    for sel in (
        "[data-testid='change-dates-checkIn']",    # confirmed by DOM inspection
        "button[aria-label*='Check-in']",           # confirmed working fallback
        "[data-testid='book_it_check_in_trigger']",
        "button[aria-label*='Check in']",
        "button[aria-label*='check in']",
    ):
        btn = page.query_selector(sel)
        if btn:
            try:
                # no_wait_after=True → don't block if the click triggers a navigation
                # timeout=3_000    → bail if element isn't clickable within 3 s
                btn.click(timeout=3_000, no_wait_after=True)
                page.wait_for_timeout(1_500)
                return True
            except Exception:
                pass
    return False


def _click_next_month(page) -> bool:
    for sel in (
        "[data-testid='calendar-next-month']",
        "[aria-label*='next month']",
        "[aria-label*='Next month']",
        "[aria-label*='Move forward']",
        "button[aria-label*='next']",
    ):
        btn = page.query_selector(sel)
        if btn:
            try:
                btn.click()
                page.wait_for_timeout(1_000)
                return True
            except Exception:
                pass
    return False


def scrape_availability(page, url: str, months: int) -> dict:
    availability: dict[str, str] = {}
    page.add_init_script(STEALTH_JS)
    page.goto(url, wait_until="domcontentloaded", timeout=40_000)
    page.wait_for_timeout(4_000)

    try:
        page.keyboard.press("Escape")
        page.wait_for_timeout(400)
    except Exception:
        pass

    # Reopen the full popup calendar (Escape may have closed it)
    _open_calendar(page)

    def harvest_dom() -> int:
        count = 0
        for cell in page.query_selector_all("[data-testid^='calendar-day-']"):
            tid = cell.get_attribute("data-testid") or ""
            ds  = _parse_testid_date(tid)
            if ds:
                blocked  = cell.get_attribute("data-is-day-blocked") == "true"
                disabled = cell.get_attribute("aria-disabled") == "true"
                availability[ds] = "unavailable" if (blocked or disabled) else "available"
                count += 1
        return count

    harvest_dom()

    today = date.today()
    for _ in range(12):
        if today.isoformat() in availability:
            break
        if not _click_next_month(page):
            break
        harvest_dom()

    for _ in range(months - 1):
        if not _click_next_month(page):
            break
        harvest_dom()

    # Only keep dates within the requested window [today, today + months]
    cutoff = _add_months(today, months).isoformat()
    return {k: v for k, v in availability.items()
            if today.isoformat() <= k <= cutoff}


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser(description="Airbnb Daily Snapshot Tracker")
    ap.add_argument("--months", "-m", type=int, default=MONTHS_TO_CHECK,
                    help=f"Months ahead to snapshot (default {MONTHS_TO_CHECK})")
    ap.add_argument("--db", default=str(DB_PATH),
                    help=f"SQLite database path (default: {DB_PATH})")
    args = ap.parse_args()

    entries = load_listings(LISTINGS_FILE)
    if not entries:
        print("[ERROR] No listings in listings.txt")
        sys.exit(1)

    conn     = init_db(Path(args.db))
    today    = date.today()
    today_s  = today.isoformat()
    cutoff_s = _add_months(today, args.months).isoformat()

    print(f"\nAirbnb Daily Tracker  —  snapshot for {today_s}")
    print(f"{'='*55}")
    print(f"Listings  : {len(entries)}")
    print(f"Window    : next {args.months} month(s)  ({today_s} → {cutoff_s})\n")

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--disable-features=Translate",
                "--disable-dev-shm-usage",
                "--no-first-run",
                "--disable-infobars",
                "--disable-extensions",
            ],
        )
        ctx = browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/122.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1400, "height": 900},
            locale="en-US",
            timezone_id="America/New_York",
        )

        for i, (lid, url) in enumerate(entries, 1):
            print(f"[{i}/{len(entries)}] Listing {lid}")
            print(f"  Scraping …", end=" ", flush=True)
            page = ctx.new_page()
            try:
                availability = scrape_availability(page, url, args.months)
                if not availability:
                    print("No data (blocked or error)")
                else:
                    rows = save_snapshot(conn, today_s, lid, availability)
                    unavail = sum(1 for s in availability.values() if s == "unavailable")
                    avail   = sum(1 for s in availability.values() if s == "available")
                    print(f"OK  — {rows} dates saved  "
                          f"({unavail} blocked, {avail} available)")
            except PWTimeout:
                print("TIMEOUT")
            except Exception as exc:
                print(f"ERROR: {exc}")
            finally:
                page.close()

            if i < len(entries):
                time.sleep(DELAY_SECS)

        ctx.close()
        browser.close()

    conn.close()
    print(f"\n✔  Snapshot saved → {Path(args.db).resolve()}")
    print(f"   Run  python report.py  to analyse bookings.\n")


if __name__ == "__main__":
    main()
