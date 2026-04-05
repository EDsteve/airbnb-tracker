"""
Airbnb Dashboard Generator
---------------------------
Reads tracker.db, runs booking-detection analysis, and writes a
self-contained dashboard.html with interactive Chart.js charts.

Usage:
    python dashboard.py            # generates dashboard.html
    python dashboard.py --open     # generate + open in default browser
    python dashboard.py --out path/to/file.html
"""

import argparse
import json
import re
import sqlite3
import webbrowser
from collections import defaultdict
from datetime import date
from pathlib import Path
from urllib.request import Request, urlopen
from urllib.error import URLError

DB_PATH  = Path(__file__).parent / "tracker.db"
OUT_HTML = Path(__file__).parent / "dashboard.html"

PALETTE = [
    "#FF5A5F", "#00A699", "#FC642D", "#484848",
    "#FFB400", "#8CE071", "#7B0051", "#00D1C1",
]


# ── Helpers ───────────────────────────────────────────────────────────────────

def short_id(lid: str) -> str:
    return f"…{lid[-8:]}" if len(lid) > 10 else lid


CACHE_FILE = Path(__file__).parent / "thumbnail_cache.json"

def _load_cache() -> dict:
    if CACHE_FILE.exists():
        try:
            return json.loads(CACHE_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}

def _save_cache(cache: dict) -> None:
    CACHE_FILE.write_text(json.dumps(cache, indent=2, ensure_ascii=False), encoding="utf-8")

def fetch_thumbnail(lid: str, cache: dict) -> str | None:
    """Return the Airbnb OG image URL for a listing, using/updating cache."""
    if lid in cache:
        return cache[lid]

    url = f"https://www.airbnb.com/rooms/{lid}"
    try:
        req = Request(url, headers={
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            )
        })
        with urlopen(req, timeout=10) as resp:
            html = resp.read().decode("utf-8", errors="replace")
        m = re.search(r'<meta\s+property=["\']og:image["\']\s+content=["\'](https?://[^"\']+)["\']', html)
        if not m:
            m = re.search(r'<meta\s+content=["\'](https?://[^"\']+)["\']\s+property=["\']og:image["\']', html)
        img = m.group(1) if m else None
    except (URLError, Exception) as e:
        print(f"  [thumbnail] Could not fetch {lid}: {e}")
        img = None

    cache[lid] = img
    return img


def airbnb_url(lid: str) -> str:
    return f"https://www.airbnb.com/rooms/{lid}"


# ── Data loading ──────────────────────────────────────────────────────────────

def load_data(db_path: Path) -> dict:
    conn = sqlite3.connect(str(db_path))

    snap_count = conn.execute(
        "SELECT COUNT(DISTINCT check_date) FROM snapshots"
    ).fetchone()[0]

    r = conn.execute(
        "SELECT MIN(check_date), MAX(check_date) FROM snapshots"
    ).fetchone()
    first_snap, last_snap = (r[0] or "—"), (r[1] or "—")

    latest = conn.execute(
        "SELECT MAX(check_date) FROM snapshots"
    ).fetchone()[0]

    # Occupancy from the most recent snapshot
    occupancy: dict[str, dict] = {}
    if latest:
        for lid, blocked, avail in conn.execute("""
            SELECT listing_id,
                   SUM(CASE WHEN status='unavailable' THEN 1 ELSE 0 END),
                   SUM(CASE WHEN status='available'   THEN 1 ELSE 0 END)
            FROM snapshots
            WHERE check_date = ?
              AND CAST(SUBSTR(calendar_date, 6, 2) AS INTEGER) BETWEEN 1 AND 12
            GROUP BY listing_id
        """, [latest]):
            b, a = (blocked or 0), (avail or 0)
            total = b + a
            occupancy[lid] = {
                "blocked":   b,
                "available": a,
                "pct": round(100 * b / total, 1) if total else 0,
            }

    # Tracked-period occupancy: for each calendar date in [first_snap … last_snap],
    # read its status from the last snapshot that actually contained it
    # (check_date <= calendar_date).  This gives a true picture of how many days
    # in the tracking window were booked on the day they passed.
    period_occ: dict[str, dict] = {}
    if latest and first_snap and last_snap and first_snap != last_snap:
        for lid, blocked, total in conn.execute("""
            SELECT s.listing_id,
                   SUM(CASE WHEN s.status='unavailable' THEN 1 ELSE 0 END),
                   COUNT(*)
            FROM snapshots s
            INNER JOIN (
                SELECT listing_id, calendar_date, MAX(check_date) AS latest_check
                FROM snapshots
                WHERE calendar_date BETWEEN ? AND ?
                  AND check_date <= calendar_date
                GROUP BY listing_id, calendar_date
            ) m ON s.listing_id    = m.listing_id
               AND s.calendar_date = m.calendar_date
               AND s.check_date    = m.latest_check
            GROUP BY s.listing_id
        """, [first_snap, last_snap]):
            t = total or 0
            b = blocked or 0
            period_occ[lid] = {
                "blocked": b,
                "total":   t,
                "pct":     round(100 * b / t, 1) if t else 0,
            }

    # Booking detection — available → unavailable transitions
    rows = conn.execute("""
        SELECT check_date, listing_id, calendar_date, status
        FROM snapshots
        WHERE CAST(SUBSTR(calendar_date, 6, 2) AS INTEGER) BETWEEN 1 AND 12
        ORDER BY listing_id, calendar_date, check_date
    """).fetchall()

    groups: dict[tuple, list] = defaultdict(list)
    for chk, lid, cal, st in rows:
        groups[(lid, cal)].append((chk, st))

    bookings = []
    for (lid, cal_date), timeline in groups.items():
        try:
            date.fromisoformat(cal_date)
        except ValueError:
            continue

        first_avail = next((c for c, s in timeline if s == "available"), None)
        if not first_avail:
            continue

        detected = next(
            (c for c, s in timeline if c > first_avail and s == "unavailable"),
            None,
        )
        if not detected:
            continue

        lead = (date.fromisoformat(cal_date) - date.fromisoformat(detected)).days
        bookings.append({
            "lid":         lid,
            "cal":         cal_date,
            "first_avail": first_avail,
            "detected":    detected,
            "lead":        lead,
        })

    bookings.sort(key=lambda r: (r["lid"], r["cal"]))
    conn.close()

    return {
        "snap_count": snap_count,
        "first_snap": first_snap,
        "last_snap":  last_snap,
        "occupancy":  occupancy,
        "period_occ": period_occ,
        "bookings":   bookings,
    }


# ── Build JS payload ──────────────────────────────────────────────────────────

def build_payload(raw: dict) -> dict:
    occupancy  = raw["occupancy"]
    period_occ = raw["period_occ"]
    bookings   = raw["bookings"]
    first_snap = raw["first_snap"]
    last_snap  = raw["last_snap"]

    all_lids = sorted(set(list(occupancy) + [b["lid"] for b in bookings]))
    colors   = {lid: PALETTE[i % len(PALETTE)] for i, lid in enumerate(all_lids)}

    by_lid: dict[str, list] = defaultdict(list)
    for b in bookings:
        by_lid[b["lid"]].append(b)

    # Fetch thumbnails (with cache)
    print("  Fetching listing thumbnails…")
    cache = _load_cache()
    thumbnails: dict[str, str | None] = {}
    for lid in all_lids:
        img = fetch_thumbnail(lid, cache)
        thumbnails[lid] = img
        status = "✔" if img else "✘"
        print(f"    {status} {lid}")
    _save_cache(cache)

    # Summary cards
    cards = []
    for lid in all_lids:
        bl    = by_lid.get(lid, [])
        leads = [b["lead"] for b in bl if b["lead"] >= 0]
        occ   = occupancy.get(lid, {})
        per   = period_occ.get(lid, {})
        cards.append({
            "lid":        lid,
            "short":      short_id(lid),
            "color":      colors[lid],
            "url":        airbnb_url(lid),
            "img":        thumbnails.get(lid),
            "nights":     len(bl),
            "avg_lead":   round(sum(leads) / len(leads), 1) if leads else None,
            "occ_pct":    occ.get("pct",       0),
            "blocked":    occ.get("blocked",   0),
            "available":  occ.get("available", 0),
            "period_pct": per.get("pct",    None),
            "period_blocked": per.get("blocked", 0),
            "period_total":   per.get("total",   0),
        })

    # Lead-time bins
    bins = [0] * 5
    for b in bookings:
        l = b["lead"]
        if l < 0:       continue
        if l <= 14:     bins[0] += 1
        elif l <= 30:   bins[1] += 1
        elif l <= 60:   bins[2] += 1
        elif l <= 90:   bins[3] += 1
        else:           bins[4] += 1

    # Timeline scatter (one dataset per listing)
    timeline = [
        {
            "label":  short_id(lid),
            "color":  colors[lid],
            "points": [{"x": b["cal"], "y": b["detected"]}
                       for b in by_lid.get(lid, [])],
        }
        for lid in all_lids
    ]

    # Period-occupancy chart (shown when period_occ has data)
    has_period = len(period_occ) > 0

    return {
        "meta": {
            "snap_count": raw["snap_count"],
            "first_snap": first_snap,
            "last_snap":  last_snap,
            "listings":   len(all_lids),
            "total":      len(bookings),
            "has_data":   len(bookings) > 0,
            "has_period": has_period,
        },
        "cards": cards,
        "chart1": {
            "labels": [short_id(l) for l in all_lids],
            "data":   [len(by_lid.get(l, [])) for l in all_lids],
            "colors": [colors[l] for l in all_lids],
        },
        "chart2": {
            "labels": ["0–14 d", "15–30 d", "31–60 d", "61–90 d", "90+ d"],
            "data":   bins,
        },
        "chart4": {
            "labels": [short_id(l) for l in all_lids],
            "data":   [period_occ.get(l, {}).get("pct", 0) for l in all_lids],
            "colors": [colors[l] for l in all_lids],
            "blocked": [period_occ.get(l, {}).get("blocked", 0) for l in all_lids],
            "total":   [period_occ.get(l, {}).get("total",   0) for l in all_lids],
        },
        "timeline": timeline,
        "table": [
            {**b, "short": short_id(b["lid"]), "color": colors[b["lid"]],
             "url": airbnb_url(b["lid"])}
            for b in sorted(bookings, key=lambda r: r["cal"])
        ],
    }


# ── HTML template (data injected as window constant) ─────────────────────────

HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>Airbnb Booking Tracker</title>
  <link rel="stylesheet"
        href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/css/bootstrap.min.css">
  <script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.3/dist/chart.umd.min.js"></script>
  <script src="https://cdn.jsdelivr.net/npm/chartjs-adapter-date-fns@3.0.0/dist/chartjs-adapter-date-fns.bundle.min.js"></script>
  <style>
    body { background: #f5f5f5; font-family: 'Segoe UI', system-ui, sans-serif; }
    .topbar { background: #FF5A5F; padding: .8rem 0; color: #fff;
              box-shadow: 0 2px 8px rgba(0,0,0,.2); margin-bottom: 2rem; }
    .topbar .brand { font-weight: 800; font-size: 1.2rem; letter-spacing: -.3px; }
    .pill { background: rgba(255,255,255,.25); border-radius: 2rem;
            padding: .25rem .9rem; font-size: .8rem; font-weight: 600; }
    .card-listing { border: none; border-radius: 12px;
                    box-shadow: 0 2px 10px rgba(0,0,0,.08); overflow: hidden; }
    .card-thumb { width: 100%; height: 140px; object-fit: cover;
                  display: block; transition: opacity .2s; }
    .card-thumb:hover { opacity: .85; }
    .card-thumb-placeholder { width: 100%; height: 140px;
                               background: #f0f0f0; display: flex;
                               align-items: center; justify-content: center;
                               font-size: 2.5rem; color: #ccc; }
    .accent { height: 5px; }
    .airbnb-link { font-size: .75rem; color: #FF5A5F; text-decoration: none;
                   font-weight: 600; }
    .airbnb-link:hover { text-decoration: underline; }
    .kv { display: flex; justify-content: space-between;
          margin-bottom: .3rem; font-size: .85rem; }
    .kv .lbl { color: #999; }
    .chart-box { background: #fff; border-radius: 12px; padding: 1.5rem;
                 box-shadow: 0 2px 8px rgba(0,0,0,.07); height: 100%; }
    .sec { font-size: .7rem; font-weight: 700; text-transform: uppercase;
           letter-spacing: .1em; color: #aaa; margin-bottom: 1rem; }
    .table { font-size: .83rem; }
    .table thead th { font-size: .68rem; text-transform: uppercase;
                      letter-spacing: .06em; color: #bbb;
                      border-bottom: 2px solid #eee; background: #fafafa; }
  </style>
</head>
<body>

<!-- Top bar -->
<div class="topbar">
  <div class="container d-flex align-items-center justify-content-between flex-wrap gap-2">
    <span class="brand">🏠 Airbnb Booking Tracker</span>
    <div class="d-flex gap-2 flex-wrap" id="metaPills"></div>
  </div>
</div>

<div class="container pb-5">

  <!-- No-data alert (shown by JS when has_data=false) -->
  <div class="alert alert-info d-none" id="noDataAlert">
    📭 <strong>No booking data yet.</strong>
    Run <code>python tracker.py</code> daily to collect snapshots.
    Booking transitions appear after <strong>at least 2 consecutive daily runs</strong>.
  </div>

  <!-- Per-listing cards -->
  <p class="sec">Per-listing overview</p>
  <div class="row g-3 mb-4" id="cardsRow"></div>

  <!-- Period-occupancy chart (shown as soon as ≥2 snapshots exist) -->
  <div id="periodSection" class="d-none mb-4">
    <div class="chart-box">
      <p class="sec">
        Tracked-period occupancy
        <small class="text-muted fw-normal ms-1" id="periodSubtitle"></small>
      </p>
      <canvas id="chart4" style="max-height:260px"></canvas>
    </div>
  </div>

  <!-- Charts (hidden until there's data) -->
  <div id="chartsSection" class="d-none">

    <div class="row g-3 mb-4">
      <div class="col-md-4">
        <div class="chart-box">
          <p class="sec">Nights Detected per Listing</p>
          <canvas id="chart1"></canvas>
        </div>
      </div>
      <div class="col-md-8">
        <div class="chart-box">
          <p class="sec">Lead Time Distribution — days before check-in when booking was detected</p>
          <canvas id="chart2"></canvas>
        </div>
      </div>
    </div>

    <div class="chart-box mb-4">
      <p class="sec">
        Booking Detection Timeline
        <small class="text-muted fw-normal ms-1">x = check-in date &nbsp;·&nbsp; y = date booking was detected</small>
      </p>
      <canvas id="chart3" style="max-height:320px"></canvas>
    </div>

    <div class="chart-box">
      <p class="sec">
        All Detected Booking Events
        <span class="badge text-bg-secondary ms-1" id="totalBadge"></span>
      </p>
      <div class="table-responsive">
        <table class="table table-hover table-sm align-middle mb-0">
          <thead>
            <tr>
              <th>Listing</th>
              <th>Check-in date</th>
              <th>First seen available</th>
              <th>Booking detected on</th>
              <th class="text-end">Lead days</th>
            </tr>
          </thead>
          <tbody id="tableBody"></tbody>
        </table>
      </div>
    </div>

  </div><!-- /chartsSection -->

</div><!-- /container -->

<script>
/* ── Data injected by dashboard.py ── */
const D = __DATA_JSON__;

window.addEventListener('DOMContentLoaded', () => {
  const m = D.meta;

  /* Meta pills */
  const pills = document.getElementById('metaPills');
  [m.listings + ' listings', m.snap_count + ' snapshots',
   m.first_snap + ' → ' + m.last_snap].forEach(t => {
    const s = document.createElement('span');
    s.className = 'pill'; s.textContent = t;
    pills.appendChild(s);
  });

  /* No-data / has-data toggle */
  if (!m.has_data) {
    document.getElementById('noDataAlert').classList.remove('d-none');
  } else {
    document.getElementById('chartsSection').classList.remove('d-none');
  }
  document.getElementById('totalBadge').textContent = m.total + ' events';

  /* ── Summary cards ── */
  const cardsRow = document.getElementById('cardsRow');
  D.cards.forEach(c => {
    const lead = c.avg_lead != null ? c.avg_lead + ' d' : '—';
    const thumb = c.img
      ? `<a href="${c.url}" target="_blank" rel="noopener">
           <img src="${c.img}" class="card-thumb" alt="Listing photo" loading="lazy">
         </a>`
      : `<a href="${c.url}" target="_blank" rel="noopener" style="text-decoration:none">
           <div class="card-thumb-placeholder">🏠</div>
         </a>`;
    const periodRow = (m.has_period && c.period_pct != null) ? `
            <hr style="margin:.5rem 0;border-color:#f0f0f0">
            <div class="kv">
              <span class="lbl" title="% of days between ${m.first_snap} and ${m.last_snap} that were blocked">Period occupancy</span>
              <strong>${c.period_pct}%</strong>
            </div>
            <div class="progress" style="height:5px">
              <div class="progress-bar" style="width:${c.period_pct}%;background:${c.color}"></div>
            </div>
            <div class="d-flex justify-content-between mt-1" style="font-size:.72rem;color:#bbb">
              <span>${c.period_blocked} of ${c.period_total} d booked</span>
            </div>` : '';
    cardsRow.insertAdjacentHTML('beforeend', `
      <div class="col-6 col-md-4 col-xl-3">
        <div class="card card-listing">
          ${thumb}
          <div class="accent" style="background:${c.color}"></div>
          <div class="card-body">
            <div class="d-flex justify-content-between align-items-start mb-2">
              <div class="fw-bold" style="color:${c.color}" title="${c.lid}">${c.short}</div>
              <a href="${c.url}" target="_blank" rel="noopener" class="airbnb-link">↗ Airbnb</a>
            </div>
            <div class="kv"><span class="lbl">Nights detected</span><strong>${c.nights}</strong></div>
            <div class="kv"><span class="lbl">Avg lead time</span><strong>${lead}</strong></div>
            <div class="kv"><span class="lbl">Occupancy now</span><strong>${c.occ_pct}%</strong></div>
            <div class="progress mt-2" style="height:5px">
              <div class="progress-bar" style="width:${c.occ_pct}%;background:${c.color}"></div>
            </div>
            <div class="d-flex justify-content-between mt-1" style="font-size:.72rem;color:#bbb">
              <span>${c.blocked} blocked</span>
              <span>${c.available} open</span>
            </div>
            ${periodRow}
          </div>
        </div>
      </div>`);
  });

  /* ── Period-occupancy section & chart4 ── */
  if (m.has_period) {
    document.getElementById('periodSection').classList.remove('d-none');
    document.getElementById('periodSubtitle').textContent =
      '% of days booked between ' + m.first_snap + ' and ' + m.last_snap;
    new Chart(document.getElementById('chart4'), {
      type: 'bar',
      data: {
        labels: D.chart4.labels,
        datasets: [{
          label: 'Period occupancy %',
          data:  D.chart4.data,
          backgroundColor: D.chart4.colors,
          borderRadius: 6,
        }]
      },
      options: {
        indexAxis: 'y',
        plugins: {
          legend: { display: false },
          tooltip: {
            callbacks: {
              label: (ctx) => {
                const i = ctx.dataIndex;
                return ` ${ctx.parsed.x}%  (${D.chart4.blocked[i]} of ${D.chart4.total[i]} days booked)`;
              }
            }
          }
        },
        scales: {
          x: {
            beginAtZero: true,
            max: 100,
            ticks: { callback: v => v + '%' }
          }
        }
      }
    });
  }

  if (!m.has_data) return;

  /* ── Table ── */
  const tbody = document.getElementById('tableBody');
  D.table.forEach(r => {
    const lead = r.lead >= 0
      ? `<span class="badge" style="background:${r.color}">${r.lead} d</span>`
      : '—';
    tbody.insertAdjacentHTML('beforeend', `
      <tr>
        <td>
          <a href="${r.url}" target="_blank" rel="noopener" style="text-decoration:none">
            <span class="badge" style="background:${r.color}">${r.short}</span>
            <span style="font-size:.7rem;color:#FF5A5F;margin-left:3px">↗</span>
          </a>
        </td>
        <td>${r.cal}</td>
        <td>${r.first_avail}</td>
        <td>${r.detected}</td>
        <td class="text-end">${lead}</td>
      </tr>`);
  });

  /* ── Chart 1 — Nights per listing (horizontal bar) ── */
  new Chart(document.getElementById('chart1'), {
    type: 'bar',
    data: {
      labels: D.chart1.labels,
      datasets: [{
        label: 'Nights detected',
        data: D.chart1.data,
        backgroundColor: D.chart1.colors,
        borderRadius: 6,
      }]
    },
    options: {
      indexAxis: 'y',
      plugins: { legend: { display: false } },
      scales: { x: { beginAtZero: true, ticks: { precision: 0 } } }
    }
  });

  /* ── Chart 2 — Lead time histogram ── */
  new Chart(document.getElementById('chart2'), {
    type: 'bar',
    data: {
      labels: D.chart2.labels,
      datasets: [{
        label: 'Bookings',
        data: D.chart2.data,
        backgroundColor: '#FF5A5F',
        borderRadius: 6,
      }]
    },
    options: {
      plugins: { legend: { display: false } },
      scales: { y: { beginAtZero: true, ticks: { precision: 0 } } }
    }
  });

  /* ── Chart 3 — Timeline scatter ── */
  new Chart(document.getElementById('chart3'), {
    type: 'scatter',
    data: {
      datasets: D.timeline.map(ds => ({
        label: ds.label,
        data:  ds.points,
        backgroundColor: ds.color + 'bb',
        borderColor:     ds.color,
        pointRadius: 5,
        pointHoverRadius: 8,
      }))
    },
    options: {
      plugins: { legend: { position: 'right' } },
      scales: {
        x: {
          type: 'time',
          time: { unit: 'month', tooltipFormat: 'yyyy-MM-dd' },
          title: { display: true, text: 'Check-in date' }
        },
        y: {
          type: 'time',
          time: { unit: 'week', tooltipFormat: 'yyyy-MM-dd' },
          title: { display: true, text: 'Date booking detected' }
        }
      }
    }
  });

}); /* DOMContentLoaded */
</script>
</body>
</html>
"""


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser(description="Airbnb Dashboard Generator")
    ap.add_argument("--db",   default=str(DB_PATH),
                    help=f"SQLite DB path (default: {DB_PATH})")
    ap.add_argument("--out",  default=str(OUT_HTML),
                    help=f"Output HTML path (default: {OUT_HTML})")
    ap.add_argument("--open", dest="open_browser", action="store_true",
                    help="Open dashboard in default browser after generating")
    args = ap.parse_args()

    db  = Path(args.db)
    out = Path(args.out)

    if not db.exists():
        print(f"[ERROR] {db} not found — run  python tracker.py  first.")
        raise SystemExit(1)

    raw     = load_data(db)
    payload = build_payload(raw)
    html    = HTML.replace("__DATA_JSON__", json.dumps(payload, ensure_ascii=False, indent=2))

    out.write_text(html, encoding="utf-8")
    print(f"✔  Dashboard written → {out.resolve()}")

    if args.open_browser:
        webbrowser.open(out.resolve().as_uri())
        print("   Opened in browser.")


if __name__ == "__main__":
    main()
