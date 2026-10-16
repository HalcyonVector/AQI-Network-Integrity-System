"""
Build a self-contained static dashboard (dashboard/index.html): no server, no JavaScript, no
external requests. Charts are inline SVG.

Leads with PIPELINE HEALTH because this project's worst problems were silent data holes (seven
fire-data days with zero detections, an API dead for weeks, a satellite product publishing 4-7
days late). Findings are shown next to their caveats, not instead of them.

Everything that comes from data (station names, reasons) is HTML-escaped.

Usage:
    python scripts/build_dashboard.py [--today YYYY-MM-DD]
"""

from __future__ import annotations

import argparse
import glob
import html
import re
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parent.parent
RAW_DIR = PROJECT_ROOT / "data" / "raw"
PROC_DIR = PROJECT_ROOT / "data" / "processed"
OUT_PATH = PROJECT_ROOT / "dashboard" / "index.html"

# name, file pattern, what the date in the filename means, normal publication lag in days
SOURCES = [
    ("CPCB feed (data.gov.in)", "cpcb_ncr_*.csv", "fetch date", 1),
    ("OpenAQ archive", "openaq_hourly_*.csv", "observation date", 7),
    ("NASA FIRMS fires", "firms_acq_*.csv", "acquisition date", 1),
    ("Satellite AOD", "aod_ncr_*.csv", "observation date", 8),
    ("ERA5 weather", "era5_wind_*.csv", "observation date", 6),
]
STALE_GRACE_DAYS = 3  # beyond lag + this, a source is "stale"
DOWN_AFTER_DAYS = 10  # beyond lag + this, a source is "down"
HOLE_WINDOW_DAYS = 30
EXPOSURE_COLUMNS = ["hit_frp_925", "hit_frp_k1.0"]  # first one present wins (set in the analysis)


# ---------------------------------------------------------------- pipeline health


def file_dates(pattern: str, raw_dir: Path = RAW_DIR) -> list[date]:
    out = []
    for f in glob.glob(str(raw_dir / pattern)):
        m = re.search(r"(\d{4}-\d{2}-\d{2})\.csv$", f)
        if m:
            out.append(datetime.strptime(m.group(1), "%Y-%m-%d").date())
    return sorted(set(out))


def source_status(dates: list[date], lag_days: int, today: date) -> dict:
    """Status of one source: ok / gaps / stale / down / none, plus its newest date and holes (missing
    dates between the first of the last HOLE_WINDOW_DAYS and the newest)."""
    if not dates:
        return {"status": "none", "latest": None, "age_days": None, "holes": []}
    latest = dates[-1]
    age = (today - latest).days
    status = "ok" if age <= lag_days + STALE_GRACE_DAYS else ("stale" if age <= lag_days + DOWN_AFTER_DAYS else "down")
    start = max(dates[0], latest - timedelta(days=HOLE_WINDOW_DAYS))
    have = set(dates)
    holes, d = [], start
    while d <= latest:
        if d not in have:
            holes.append(d)
        d += timedelta(days=1)
    if status == "ok" and holes:
        # Fresh newest file, but days are missing behind it: green would be a lie.
        status = "gaps"
    return {"status": status, "latest": latest, "age_days": age, "holes": holes}


def all_source_health(today: date, raw_dir: Path = RAW_DIR) -> list[dict]:
    rows = []
    for name, pattern, meaning, lag in SOURCES:
        s = source_status(file_dates(pattern, raw_dir), lag, today)
        rows.append({"name": name, "meaning": meaning, "lag": lag, **s})
    return rows


# ---------------------------------------------------------------- charts (inline SVG)


def _scale(v, lo, hi, a, b):
    return a if hi == lo else a + (v - lo) / (hi - lo) * (b - a)


def line_bar_svg(dates: list[str], line: list[float | None], bars: list[float | None],
                 line_label: str, bar_label: str, width: int = 760, height: int = 280) -> str:
    """Line (left axis) over bars (right axis), as an SVG string. None values leave gaps."""
    ml, mr, mt, mb = 52, 52, 18, 44
    pw, ph = width - ml - mr, height - mt - mb
    n = len(dates)
    if n == 0:
        return '<p class="muted">No data to chart.</p>'
    lvals = [v for v in line if v is not None]
    bvals = [v for v in bars if v is not None]
    lmax = max(lvals) * 1.1 if lvals else 1
    bmax = max(bvals) * 1.1 if bvals else 1
    step = pw / n
    x = lambda i: ml + step * (i + 0.5)  # noqa: E731
    parts = [f'<svg viewBox="0 0 {width} {height}" role="img" aria-label="{html.escape(line_label)} and {html.escape(bar_label)}" class="chart">']
    for frac in (0, 0.25, 0.5, 0.75, 1):  # grid + left-axis labels
        y = mt + ph * (1 - frac)
        parts.append(f'<line x1="{ml}" x2="{ml + pw}" y1="{y:.1f}" y2="{y:.1f}" class="grid"/>')
        parts.append(f'<text x="{ml - 6}" y="{y + 4:.1f}" class="tick" text-anchor="end">{lmax * frac:.0f}</text>')
        parts.append(f'<text x="{ml + pw + 6}" y="{y + 4:.1f}" class="tick">{bmax * frac:.0f}</text>')
    for i, v in enumerate(bars):
        if v is None:
            continue
        h = _scale(v, 0, bmax, 0, ph)
        parts.append(f'<rect x="{x(i) - step * 0.35:.1f}" y="{mt + ph - h:.1f}" width="{step * 0.7:.1f}" height="{h:.1f}" class="bar"><title>{html.escape(dates[i])}: {v:.0f}</title></rect>')
    seg, path = [], []
    for i, v in enumerate(line):
        if v is None:
            if seg:
                path.append(seg)
                seg = []
            continue
        seg.append((x(i), mt + ph - _scale(v, 0, lmax, 0, ph), dates[i], v))
    if seg:
        path.append(seg)
    for s in path:
        if len(s) > 1:
            parts.append('<polyline class="line" points="' + " ".join(f"{px:.1f},{py:.1f}" for px, py, _, _ in s) + '"/>')
        for px, py, d, v in s:
            parts.append(f'<circle cx="{px:.1f}" cy="{py:.1f}" r="3" class="dot"><title>{html.escape(d)}: {v:.1f}</title></circle>')
    every = max(1, n // 8)
    for i in range(0, n, every):
        parts.append(f'<text x="{x(i):.1f}" y="{mt + ph + 16}" class="tick" text-anchor="middle">{html.escape(dates[i][5:])}</text>')
    parts.append(f'<text x="{ml - 40}" y="{mt - 4}" class="axis">{html.escape(line_label)}</text>')
    parts.append(f'<text x="{ml + pw + 6}" y="{mt - 4}" class="axis bar-axis">{html.escape(bar_label)}</text>')
    parts.append("</svg>")
    return "".join(parts)


# ---------------------------------------------------------------- HTML pieces


def _read(name: str, proc_dir: Path = PROC_DIR) -> pd.DataFrame | None:
    p = proc_dir / name
    return pd.read_csv(p) if p.exists() else None


def health_html(rows: list[dict]) -> str:
    label = {"ok": "OK", "gaps": "GAPS", "stale": "STALE", "down": "DOWN", "none": "NO DATA"}
    out = ['<table><thead><tr><th>Source</th><th>Status</th><th>Newest</th><th>Age</th><th>Missing days (last 30)</th></tr></thead><tbody>']
    for r in rows:
        latest = r["latest"].isoformat() if r["latest"] else "-"
        age = "-" if r["age_days"] is None else f'{r["age_days"]} d (normal lag {r["lag"]} d)'
        holes = ", ".join(h.strftime("%d %b") for h in r["holes"]) or "none"
        out.append(f'<tr><td>{html.escape(r["name"])}<div class="muted">{html.escape(r["meaning"])}</div></td>'
                   f'<td><span class="chip {r["status"]}">{label[r["status"]]}</span></td><td>{latest}</td><td>{html.escape(age)}</td>'
                   f'<td class="holes">{html.escape(holes)}</td></tr>')
    out.append("</tbody></table>")
    return "".join(out)


def stations_html(health: pd.DataFrame | None) -> str:
    if health is None or health.empty:
        return '<p class="muted">Station-health results not available.</p>'
    shown = health[health["level"].isin(["flag", "watch"])]
    if shown.empty:
        return "<p>No stations flagged or on watch.</p>"
    out = ['<table><thead><tr><th>Station</th><th>Level</th><th>Why</th></tr></thead><tbody>']
    for _, r in shown.iterrows():
        out.append(f'<tr><td>{html.escape(str(r["station"]))}</td><td><span class="chip {html.escape(str(r["level"]))}">{html.escape(str(r["level"]).upper())}</span></td>'
                   f'<td>{html.escape(str(r["reason"]))}</td></tr>')
    out.append("</tbody></table>")
    return "".join(out)


def decoupling_html(df: pd.DataFrame | None) -> str:
    if df is None or df.empty:
        return '<p class="muted">Satellite-consistency results not available.</p>'
    flagged = df[df["flag"]]
    watch = df[df["reason"].astype(str).str.startswith("watch")]
    n = len(df)
    head = f"<p>{n} stations tested; <b>{len(flagged)} flagged</b>, {len(watch)} borderline.</p>"
    if watch.empty and flagged.empty:
        return head
    out = [head, '<table><thead><tr><th>Station</th><th>Days</th><th>Correlation with satellite</th><th>q-value</th></tr></thead><tbody>']
    for _, r in pd.concat([flagged, watch]).drop_duplicates("station").iterrows():
        out.append(f'<tr><td>{html.escape(str(r["station"]))}</td><td>{int(r["n_days"])}</td><td>{r["rho"]:+.2f} (network median {r["network_median_rho"]:.2f})</td><td>{r["q_value"]:.2f}</td></tr>')
    out.append("</tbody></table>")
    return "".join(out)


def stubble_html(daily: pd.DataFrame | None) -> str:
    if daily is None or daily.empty or "pm25_delhi" not in daily:
        return '<p class="muted">Attribution results not available.</p>'
    col = next((c for c in EXPOSURE_COLUMNS if c in daily.columns), None)
    d = daily.dropna(subset=["pm25_delhi"]).sort_values("date")
    if col is None or d.empty:
        return '<p class="muted">Attribution results not available.</p>'
    return line_bar_svg(list(d["date"]), [float(v) for v in d["pm25_delhi"]], [None if pd.isna(v) else float(v) for v in d[col]],
                        "Delhi PM2.5 (ug/m3)", "Fire exposure index")


CSS = """
:root{--bg:#fff;--fg:#1c2230;--muted:#5b6578;--card:#f6f7fa;--line:#dde1ea;--accent:#c2410c;--bar:#9db4d3;--ok:#166534;--okbg:#dcfce7;--warn:#854d0e;--warnbg:#fef3c7;--bad:#991b1b;--badbg:#fee2e2}
@media (prefers-color-scheme: dark){:root{--bg:#12151c;--fg:#e6e9f0;--muted:#98a2b6;--card:#1b1f29;--line:#2c3342;--accent:#fb923c;--bar:#4a6286;--ok:#86efac;--okbg:#14301f;--warn:#fde68a;--warnbg:#3a2f10;--bad:#fca5a5;--badbg:#3b1717}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.5 system-ui,-apple-system,Segoe UI,sans-serif}
main{max-width:860px;margin:0 auto;padding:20px 16px 48px}h1{font-size:22px;margin:0 0 4px}h2{font-size:17px;margin:32px 0 8px}
.muted{color:var(--muted);font-size:13px}section{margin-top:8px}table{width:100%;border-collapse:collapse;font-size:14px}
th,td{text-align:left;padding:8px 10px;border-bottom:1px solid var(--line);vertical-align:top}th{color:var(--muted);font-weight:600;font-size:12px;text-transform:uppercase;letter-spacing:.04em}
.holes{font-size:13px;color:var(--muted)}.chip{display:inline-block;padding:1px 8px;border-radius:999px;font-size:12px;font-weight:700}
.chip.ok{background:var(--okbg);color:var(--ok)}.chip.stale,.chip.gaps,.chip.watch{background:var(--warnbg);color:var(--warn)}.chip.down,.chip.flag,.chip.none{background:var(--badbg);color:var(--bad)}
.caveat{background:var(--card);border-left:4px solid var(--accent);padding:10px 14px;border-radius:4px;margin:10px 0;font-size:14px}
.chart{width:100%;height:auto}.chart .grid{stroke:var(--line);stroke-width:1}.chart .tick{fill:var(--muted);font-size:11px}.chart .axis{fill:var(--fg);font-size:12px;font-weight:600}
.chart .bar-axis{fill:var(--muted)}.chart .bar{fill:var(--bar)}.chart .line{fill:none;stroke:var(--accent);stroke-width:2.5}.chart .dot{fill:var(--accent)}
.scroll{overflow-x:auto}
"""


def render(today: date, raw_dir: Path = RAW_DIR, proc_dir: Path = PROC_DIR) -> str:
    health = all_source_health(today, raw_dir)
    bad = [r for r in health if r["status"] in ("gaps", "stale", "down", "none")]
    banner = ("" if not bad else '<div class="caveat"><b>Attention:</b> ' + html.escape(", ".join(f'{r["name"]} is {r["status"].upper()}' for r in bad)) + ".</div>")
    stubble_daily = _read("stubble_transport_daily.csv", proc_dir)
    sections = f"""
<h1>NCR air-quality network integrity</h1>
<div class="muted">Generated {today.isoformat()} &middot; static page, no tracking, data from the project's own pipelines</div>
{banner}
<h2>1. Is the data flowing?</h2>
<div class="scroll">{health_html(health)}</div>
<p class="muted">OK = newest data is as fresh as that source normally is. GAPS = fresh, but days are missing behind it.
STALE / DOWN = nothing new for longer than usual. "Missing days" are dates in the last 30 with no file between a source's
first and newest date. A gap in the CPCB feed cannot be recovered from that feed (it serves only the current snapshot);
OpenAQ may cover some of it.</p>

<h2>2. Delhi PM2.5 and regional fire exposure</h2>
<div class="scroll">{stubble_html(stubble_daily)}</div>
<div class="caveat"><b>Read with care.</b> Delhi PM2.5 stepped up the same day regional fire plumes started arriving, but
the days before were <i>rainy</i> (about 9 mm on 26 Sep, 4 mm on 27 Sep) and 30 Sep was the first fully dry day, so rain stopping
explains the step without stubble. On rain-free days the association is moderate and not yet significant (about 10 days).
Suggestive, not established, and <b>not</b> a share of Delhi's pollution attributable to stubble burning.</div>

<h2>3. Station health (neighbour consistency)</h2>
<div class="scroll">{stations_html(_read("neighbor_station_health.csv", proc_dir))}</div>
<div class="caveat">A flag means "have someone look", not "faulty". A station beside a road or kiln legitimately differs from its
neighbours. Flatlines (a sensor repeating one exact value for hours) are the clearest evidence; drift needs weeks of data to confirm.</div>

<h2>4. Agreement with the satellite</h2>
<div class="scroll">{decoupling_html(_read("station_temporal_decoupling.csv", proc_dir))}</div>
<div class="caveat">The satellite tracks the city's day-to-day PM2.5 but not differences between stations on the same day, so it can only
check whether a station <i>follows the same day-to-day pattern as its peers</i>. With about two weeks of data, no station is
statistically distinguishable yet.</div>
"""
    return (f'<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
            f"<title>NCR AQI network integrity</title><style>{CSS}</style></head><body><main>{sections}</main></body></html>")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--today", help="YYYY-MM-DD (default: today)")
    args = parser.parse_args()
    today = datetime.strptime(args.today, "%Y-%m-%d").date() if args.today else date.today()
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(render(today), encoding="utf-8")
    print(f"Wrote {OUT_PATH} ({OUT_PATH.stat().st_size / 1024:.0f} KB)")
    for r in all_source_health(today):
        print(f"  {r['name']:26} {r['status'].upper():8} newest {r['latest']}  holes: {len(r['holes'])}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
