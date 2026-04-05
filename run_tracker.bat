@echo off
:: ============================================================
:: Airbnb Daily Tracker — Windows Task Scheduler launcher
:: ============================================================
:: Point this at your Python installation and the tracker.py
:: Then schedule it in Task Scheduler to run once per day.
:: ============================================================

cd /d "c:\Development\Airbnb"
python tracker.py >> tracker_log.txt 2>&1
