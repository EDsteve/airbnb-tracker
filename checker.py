"""
Airbnb Market Research Occupancy Checker
-----------------------------------------
Scrapes publicly visible calendar data from Airbnb listing pages using
a headless Playwright browser, then saves a CSV occupancy report.

Usage:
    python checker.py
    python checker.py --listing https://www.airbnb.com/rooms/12345678
    python checker.py --months 6
"""

import argparse
import csv
import re
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path

from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

# ── Config ───────────────────────────────────────────────────────────────────
LISTINGS_FILE   = Path(__file__).parent / "listings.txt"
OUTPUT_CSV      = Path(__file__).parent / "occupancy_report.csv"
MONTHS_TO_CHECK = 3
DELAY_SECS      = 3          # polite delay between listings

# Stealth JS injected before every page load
STEALTH_JS = """
Object.defineProperty(navigator, 'webdriver', {get: () => undefined, configurable: true});
Object.defineProperty(navigator, 'plugins',   {get: () => [1, 2, 3, 4, 5]});
Object.defineProperty(navigator, 'languages', {get: () => ['en-US', 'en']});
window.chrome = {runtime: {}, app: {isInstalled: false}};
delete window.__playwright;
delete window.__pw_manual;
"""


# ── Helpers ───────────────────────────────────────────────────────────────────

def extract_listing_id(raw: str):
    """Return (listing_id, clean_url) or (None, None)."""
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


def try_parse_date(label: str):
    """Parse a date from an Airbnb aria-label like 'Wednesday, March 5, 2026'."""
    # Drop leading weekday if present
    parts = label.split(", ")
    candidate = ", ".join(parts[-2:]) if len(parts) >= 3 else label
    candidate = candidate.split(".")[0].strip()
    for fmt in ("%B %d, %Y", "%b %d, %Y"):
        try:
            return datetime.strptime(candidate, fmt).date()
        except ValueError:
            pass
    return None


# ── Scraper ───────────────────────────────────────────────────────────────────

def _parse_testid_date(tid: str) -> str | None:
    """
    Convert Airbnb data-testid date to ISO format.
    Airbnb uses  calendar-day-MM/DD/YYYY  (e.g. calendar-day-03/17/2026)
    in en-US locale. Returns 'YYYY-MM-DD' or None.
    """
    m = re.search(r"(\d{2}/\d{2}/\d{4})", tid)
    if m:
        mm, dd, yyyy = m.group(1).split("/")
        return f"{yyyy}-{mm}-{dd}"
    # Fallback: YYYY-MM-DD embedded in testid
    m2 = re.search(r"(\d{4}-\d{2}-\d{2})", tid)
    if m2:
        return m2.group(1)
    return None


def _click_next_month(page) -> bool:
    """Click the 'next month' button; returns True if found."""
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
    """
    Load a listing page and harvest available/unavailable dates.
    Returns {YYYY-MM-DD: 'available'|'unavailable'}.
    """
    availability: dict[str, str] = {}

    # Inject stealth BEFORE the page loads
    page.add_init_script(STEALTH_JS)

    page.goto(url, wait_until="domcontentloaded", timeout=40_000)
    page.wait_for_timeout(4_000)

    # Dismiss any translate / cookie popups
    try:
        page.keyboard.press("Escape")
        page.wait_for_timeout(400)
    except Exception:
        pass

    def harvest_dom() -> int:
        count = 0
        for cell in page.query_selector_all("[data-testid^='calendar-day-']"):
            tid  = cell.get_attribute("data-testid") or ""
            ds   = _parse_testid_date(tid)
            if ds:
                # Airbnb marks blocked/booked days with data-is-day-blocked="true"
                blocked  = cell.get_attribute("data-is-day-blocked") == "true"
                # Also catch aria-disabled as a secondary signal
                disabled = cell.get_attribute("aria-disabled") == "true"
                availability[ds] = "unavailable" if (blocked or disabled) else "available"
                count += 1
        # Fallback: aria-label on td/button elements
        if count == 0:
            for cell in page.query_selector_all("td[aria-label], button[aria-label]"):
                label = cell.get_attribute("aria-label") or ""
                d = try_parse_date(label)
                if d and d >= date.today():
                    blocked  = cell.get_attribute("data-is-day-blocked") == "true"
                    disabled = cell.get_attribute("aria-disabled") == "true"
                    availability[d.isoformat()] = "unavailable" if (blocked or disabled) else "available"
                    count += 1
        return count

    harvest_dom()

    # ── Navigate forward until today's date is in the data ──
    # Airbnb's calendar often opens at a past month; click Next until
    # we reach the current month (max 12 attempts).
    today = date.today()
    for _ in range(12):
        if today.isoformat() in availability:
            break
        if not _click_next_month(page):
            break
        harvest_dom()

    # ── Harvest the requested number of additional months ──
    for _ in range(months - 1):
        if not _click_next_month(page):
            break
        harvest_dom()

    return availability


def compute_stats(availability: dict, months: int) -> dict:
    today = date.today()
    end   = today + timedelta(days=30 * months)
    total = unavail = avail = 0
    cursor = today
    while cursor <= end:
        s = availability.get(cursor.isoformat())
        if s is not None:
            total += 1
            if s == "unavailable":
                unavail += 1
            else:
                avail += 1
        cursor += timedelta(days=1)
    rate = round(unavail / total * 100, 1) if total else 0.0
    return {"total_days": total, "unavailable": unavail,
            "available": avail, "unavailability_rate%": rate}


# ── Runner ────────────────────────────────────────────────────────────────────

def run_all(entries: list, months: int) -> list[dict]:
    results = []
    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--disable-features=Translate",          # no translate popup
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

        today = date.today()
        end   = today + timedelta(days=30 * months)

        for i, (lid, url) in enumerate(entries, 1):
            print(f"[{i}/{len(entries)}] Listing {lid}")
            print(f"  Scraping …", end=" ", flush=True)
            page = ctx.new_page()
            try:
                availability = scrape_availability(page, url, months)
                if not availability:
                    print("No calendar data found (Airbnb may have blocked the request)")
                    row = {"listing_id": lid, "url": url,
                           "check_date": today.isoformat(),
                           "start_date": today.isoformat(),
                           "end_date": end.isoformat(),
                           "total_days": 0, "unavailable": 0, "available": 0,
                           "unavailability_rate%": "N/A", "status": "no_data"}
                else:
                    stats = compute_stats(availability, months)
                    print(
                        f"OK  ({stats['unavailable']} unavail / "
                        f"{stats['total_days']} days = {stats['unavailability_rate%']}%)"
                    )
                    row = {"listing_id": lid, "url": url,
                           "check_date": today.isoformat(),
                           "start_date": today.isoformat(),
                           "end_date": end.isoformat(),
                           **stats, "status": "ok"}
            except PWTimeout:
                print("TIMEOUT")
                row = {"listing_id": lid, "url": url,
                       "check_date": today.isoformat(),
                       "start_date": today.isoformat(),
                       "end_date": end.isoformat(),
                       "total_days": 0, "unavailable": 0, "available": 0,
                       "unavailability_rate%": "N/A", "status": "error:timeout"}
            except Exception as exc:
                print(f"ERROR: {exc}")
                row = {"listing_id": lid, "url": url,
                       "check_date": today.isoformat(),
                       "start_date": today.isoformat(),
                       "end_date": end.isoformat(),
                       "total_days": 0, "unavailable": 0, "available": 0,
                       "unavailability_rate%": "N/A",
                       "status": f"error:{str(exc)[:60]}"}
            finally:
                page.close()

            results.append(row)
            if i < len(entries):
                time.sleep(DELAY_SECS)

        ctx.close()
        browser.close()
    return results


def save_report(rows: list[dict], path: Path):
    if not rows:
        return
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)
    print(f"\n✔  Report saved → {path}")


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser(description="Airbnb Market Research Occupancy Checker")
    ap.add_argument("--listing", "-l", help="Single listing URL or ID")
    ap.add_argument("--months",  "-m", type=int, default=MONTHS_TO_CHECK,
                    help=f"Months ahead to analyse (default {MONTHS_TO_CHECK})")
    ap.add_argument("--output",  "-o", default=str(OUTPUT_CSV),
                    help="Output CSV path")
    args = ap.parse_args()

    if args.listing:
        lid, url = extract_listing_id(args.listing)
        if not lid:
            print(f"[ERROR] Cannot parse listing from: {args.listing!r}")
            sys.exit(1)
        entries = [(lid, url)]
    else:
        entries = load_listings(LISTINGS_FILE)

    if not entries:
        print("[ERROR] No listings to check. Edit listings.txt or use --listing.")
        sys.exit(1)

    print(f"\nAirbnb Occupancy Checker  (market-research / public scrape mode)")
    print(f"==================================================================")
    print(f"Listings  : {len(entries)}")
    print(f"Period    : next {args.months} month(s) from {date.today()}")
    print(f"Note      : 'unavailable' = booked + host-blocked combined\n")

    results = run_all(entries, args.months)
    save_report(results, Path(args.output))

    print("\n── Summary ─────────────────────────────────────────────────")
    for r in results:
        rate = r["unavailability_rate%"]
        flag = "  ⚠ no data" if r["status"] == "no_data" else \
               "  ✗ error"  if r["status"].startswith("error") else ""
        print(f"  {r['listing_id']:>20}  →  {str(rate):>5}% unavailable{flag}")
    print("────────────────────────────────────────────────────────────\n")


if __name__ == "__main__":
    main()
