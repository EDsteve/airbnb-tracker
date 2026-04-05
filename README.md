# Airbnb Listing Occupancy Tracker

A Python toolkit that scrapes publicly visible Airbnb listing calendars using a headless Chromium browser, builds a daily SQLite database of availability snapshots, and detects new bookings over time.

> ⚠️ **Note:** Scraping Airbnb may conflict with their [Terms of Service](https://www.airbnb.com/terms). Use responsibly for personal market research only.

---

## Files

| File | Purpose |
|---|---|
| `listings.txt` | Your list of Airbnb listing URLs |
| `checker.py` | One-shot occupancy snapshot → CSV report |
| `tracker.py` | **Daily runner** — saves snapshots to `tracker.db` |
| `report.py` | Analyses `tracker.db` and detects new bookings |
| `run_tracker.bat` | Windows batch launcher for Task Scheduler |

---

## Requirements

- Python 3.10+
- `playwright` + Chromium browser

```bash
pip install playwright
playwright install chromium
```

---

## Quick start — one-shot snapshot

```bash
python checker.py                                        # all listings in listings.txt
python checker.py --listing https://www.airbnb.com/rooms/12345678
python checker.py --months 6
```

Outputs `occupancy_report.csv` with unavailability % per listing.

---

## Daily tracking — detect bookings over time

### Step 1: Add your listings

Edit `listings.txt` (one URL per line):
```
https://www.airbnb.com/rooms/41386386
https://www.airbnb.com/rooms/12345678
```

### Step 2: Run the tracker daily
For example:
python tracker.py --months 3 2>&1

```bash
python tracker.py
```

Each run saves today's availability snapshot to `tracker.db`.  
**The earlier you start, the more booking transitions you'll capture.**

### Step 3: Generate a booking-detection report

```bash
python report.py                                    # all time, all listings
python report.py --from 2026-03-01 --to 2026-03-28  # specific date range
python report.py --listing 41386386                 # single listing
```

Sample output:
```
── Per-listing summary ─────────────────────────────────
  Listing  : 41386386
  Bookings : 7 nights detected
  Avg lead : 12.4 days before check-in
  Detected : 2026-03-05  →  2026-03-22

── Detailed booking events ─────────────────────────────
  Listing          Cal. date    First avail  Booked on    Lead days
  ---------------  -----------  -----------  -----------  ---------
  41386386         2026-03-10   2026-03-01   2026-03-03           7
  41386386         2026-03-11   2026-03-01   2026-03-03           8
  ...
```

---

## Automate with Windows Task Scheduler

1. Open **Task Scheduler** (search in Start menu)
2. Click **Create Basic Task…**
3. Name: `Airbnb Tracker`
4. Trigger: **Daily** at a fixed time (e.g. 8:00 AM)
5. Action: **Start a program**
   - Program: `C:\Development\Airbnb\run_tracker.bat`
6. Finish

Logs are saved to `tracker_log.txt`.

---

## How booking detection works

The tracker captures a daily snapshot showing which dates are *available* vs *unavailable* (blocked/booked) **from today onwards**.

| Day | March 15 status | Interpretation |
|---|---|---|
| March 1 | ✅ available | — |
| March 2 | ✅ available | — |
| March 3 | 🔴 unavailable | **Booking detected!** Someone booked March 15 on March 2 |

> "Unavailable" = booked by guest **+** host-blocked dates **+** minimum-stay gaps.  
> You cannot distinguish between these three from the public calendar.

---

## CSV output columns

### `occupancy_report.csv` (from checker.py)
| Column | Description |
|---|---|
| `listing_id` | Airbnb listing ID |
| `unavailability_rate%` | % of days blocked in the period |
| `unavailable` | Days blocked/booked |
| `available` | Days open to book |

### `bookings_report.csv` (from report.py)
| Column | Description |
|---|---|
| `listing_id` | Airbnb listing ID |
| `calendar_date` | The date that got booked |
| `first_seen_avail` | First snapshot date when it was available |
| `booking_detected` | Snapshot date when it turned unavailable |
| `days_before_checkin` | How far in advance the booking was made |
