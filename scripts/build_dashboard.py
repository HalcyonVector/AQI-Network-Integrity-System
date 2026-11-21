"""
Build a self-contained static dashboard (dashboard/index.html): no server, no JavaScript, no
external requests (system fonts only; charts are inline SVG; gradients are CSS/SVG).

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

import dashboard_extras as ex
import dashboard_historical as dh

PROJECT_ROOT = Path(__file__).resolve().parent.parent
RAW_DIR = PROJECT_ROOT / "data" / "raw"
PROC_DIR = PROJECT_ROOT / "data" / "processed"
OUT_PATH = PROJECT_ROOT / "dashboard" / "index.html"
LOGS_DIR = PROJECT_ROOT / "logs"
REPO_URL = "https://github.com/HalcyonVector/AQI-Network-Integrity-System"
GITHUB_URL = "https://github.com/HalcyonVector"
# Logo: a monitoring ring, a drifting air-quality wave, and three linked station nodes. Inline so the
# page stays a single file; the favicon is the same mark (xmlns is percent-encoded to keep the page
# free of any http:// address).
LOGO_SVG = ('<svg class="logo" viewBox="0 0 48 48" role="img" aria-label="AQI NIS logo">'
            '<defs><linearGradient id="lg" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stop-color="#a3d2b8"/><stop offset="1" stop-color="#5fb3d6"/></linearGradient></defs>'
            '<rect x="2" y="2" width="44" height="44" rx="13" fill="rgba(111,191,178,.10)" stroke="url(#lg)" stroke-width="1.6"/>'
            '<path d="M8 29 C13 21 17 21 22 28 S31 35 40 22" fill="none" stroke="url(#lg)" stroke-width="2.4" stroke-linecap="round"/>'
            '<path d="M14 15 L24 11 L34 16" fill="none" stroke="#a3d2b8" stroke-opacity=".55" stroke-width="1.3"/>'
            '<circle cx="14" cy="15" r="2.6" fill="#a3d2b8"/><circle cx="24" cy="11" r="2.6" fill="#dfb277"/><circle cx="34" cy="16" r="2.6" fill="#a3d2b8"/></svg>')
FAVICON = ("data:image/svg+xml,%3Csvg xmlns='http%3A//www.w3.org/2000/svg' viewBox='0 0 48 48'%3E"
           "%3Crect x='2' y='2' width='44' height='44' rx='13' fill='%230c171b' stroke='%236fbfb2' stroke-width='2'/%3E"
           "%3Cpath d='M8 29 C13 21 17 21 22 28 S31 35 40 22' fill='none' stroke='%23a3d2b8' stroke-width='3' stroke-linecap='round'/%3E"
           "%3Ccircle cx='14' cy='15' r='3' fill='%23a3d2b8'/%3E%3Ccircle cx='24' cy='11' r='3' fill='%23dfb277'/%3E%3Ccircle cx='34' cy='16' r='3' fill='%23a3d2b8'/%3E%3C/svg%3E")
LICENSE_URL = REPO_URL + "/blob/master/README.md#license"
# Footer content. Every URL was checked to resolve; with the project links they are the only external
# addresses the page carries.
# (name, url, what it provides)
FOOTER_DATA = [
    ("CPCB real-time AQI via data.gov.in", "https://www.data.gov.in/catalog/real-time-air-quality-index-various-locations", "Hourly AQI sub-indices per NCR station (the live feed)."),
    ("CPCB yearly station files (Vonter/india-cpcb-aqi, ODbL)", "https://github.com/Vonter/india-cpcb-aqi", "15-minute station records for 2015 to 2025, republished from CPCB&rsquo;s data repository."),
    ("OpenAQ", "https://docs.openaq.org", "Hourly PM2.5 concentrations from the same stations, in &micro;g/m&sup3;."),
    ("NASA FIRMS", "https://firms.modaps.eosdis.nasa.gov", "Satellite active-fire detections and their radiative power, live and the 2015 to 2025 archive."),
    ("MODIS MAIAC AOD, Earth Engine", "https://developers.google.com/earth-engine/datasets/catalog/MODIS_061_MCD19A2_GRANULES", "Aerosol optical depth, sampled at each station."),
    ("ERA5, Copernicus CDS", "https://cds.climate.copernicus.eu/datasets/reanalysis-era5-single-levels", "Wind, boundary-layer height, temperature and rain."),
]
# (citation, url)
FOOTER_REFS = [
    ("Benjamini and Hochberg (1995), controlling the false discovery rate", "https://www.jstor.org/stable/2346101"),
    ("Lyapustin et al. (2018), MAIAC aerosol retrieval for MODIS", "https://doi.org/10.5194/amt-11-5741-2018"),
    ("Giglio et al. (2016), MODIS Collection 6 active fire detection", "https://doi.org/10.1016/j.rse.2016.02.054"),
    ("Hersbach et al. (2020), the ERA5 reanalysis (dataset page)", "https://cds.climate.copernicus.eu/datasets/reanalysis-era5-single-levels"),
    ("Air quality index categories (India section)", "https://en.wikipedia.org/wiki/Air_quality_index#India"),
]
FOOTER_URLS = [GITHUB_URL, REPO_URL, LICENSE_URL] + [u for _, u, _ in FOOTER_DATA] + [u for _, u in FOOTER_REFS]


def _a(url: str, text: str) -> str:
    return f'<a href="{url}" target="_blank" rel="noopener">{text}</a>'


def footer_html() -> str:
    data = "".join(f"<li>{_a(u, html.escape(n))}<span>{d}</span></li>" for n, u, d in FOOTER_DATA)
    refs = "".join(f"<li>{_a(u, html.escape(n))}</li>" for n, u in FOOTER_REFS)
    return (
        '<footer class="foot"><div class="foot-grid">'
        '<div><h3>About</h3><p>Checks whether the NCR air-quality monitoring network can be trusted: data health, station drift, '
        'gap filling and fire evidence. It is exploratory analysis of a few weeks of data, not an official AQI or a health advisory. '
        f'{_a(REPO_URL, "Source code")}.</p></div>'
        f"<div><h3>Data</h3><ul>{data}</ul></div>"
        f"<div><h3>References</h3><ul>{refs}</ul></div></div>"
        '<div class="foot-bar"><span>Built by <b>Sagnik</b></span>'
        f'{_a(GITHUB_URL, "GitHub @HalcyonVector")}{_a(REPO_URL, "Repository")}{_a(LICENSE_URL, "License")}</div></footer>')

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
PM25_STANDARD = 60.0  # India's NAAQS 24-hour PM2.5 limit, ug/m3


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
        return {"status": "none", "latest": None, "age_days": None, "holes": [], "dates": []}
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
    return {"status": status, "latest": latest, "age_days": age, "holes": holes, "dates": dates}


def all_source_health(today: date, raw_dir: Path = RAW_DIR) -> list[dict]:
    rows = []
    for name, pattern, meaning, lag in SOURCES:
        s = source_status(file_dates(pattern, raw_dir), lag, today)
        rows.append({"name": name, "meaning": meaning, "lag": lag, **s})
    return rows


def day_strip(dates: list[date], today: date, n: int = HOLE_WINDOW_DAYS) -> str:
    """The last n days as a row of cells. ok = a file exists; miss = no file although the source had
    started and had published later days; pend = after the newest file (not yet published, which is
    normal, so NOT shown as missing); pre = before the source's first file."""
    have = set(dates)
    first, last = (dates[0], dates[-1]) if dates else (None, None)
    cells = []
    for i in range(n - 1, -1, -1):
        d = today - timedelta(days=i)
        if d in have:
            cls = "ok"
        elif first is None or d < first:
            cls = "pre"
        elif d > last:
            cls = "pend"
        else:
            cls = "miss"
        cells.append(f'<span class="cell {cls}" title="{d.strftime("%d %b")}"></span>')
    return '<div class="strip">' + "".join(cells) + "</div>"


# ---------------------------------------------------------------- charts (inline SVG)


def _scale(v, lo, hi, a, b):
    return a if hi == lo else a + (v - lo) / (hi - lo) * (b - a)


def line_bar_svg(dates: list[str], line: list[float | None], bars: list[float | None] | None,
                 line_label: str, bar_label: str, width: int = 760, height: int = 280,
                 rain: list[float | None] | None = None, threshold: float | None = None,
                 tick_fmt=lambda s: s[5:]) -> str:
    """Line (left axis) over bars (right axis), as an SVG string. None values leave gaps.

    Optional: `rain` draws cyan bars hanging from the top (own scale, at most a third of the height);
    `threshold` draws a dashed reference line on the left axis (e.g. the PM2.5 standard)."""
    ml, mr, mt, mb = 52, 52, 30, 44
    pw, ph = width - ml - mr, height - mt - mb
    n = len(dates)
    if n == 0:
        return '<p class="muted">No data to chart.</p>'
    has_bars = bars is not None and any(b is not None for b in bars)
    bars = bars if bars is not None else [None] * n
    lvals = [v for v in line if v is not None]
    bvals = [v for v in bars if v is not None]
    lmax = max(lvals + ([threshold] if threshold else [])) * 1.12 if lvals else 1
    bmax = max(bvals) * 1.12 if bvals else 1
    step = pw / n
    x = lambda i: ml + step * (i + 0.5)  # noqa: E731
    base = mt + ph
    parts = [f'<svg viewBox="0 0 {width} {height}" role="img" aria-label="{html.escape(line_label)} and {html.escape(bar_label)}" class="chart">',
             '<defs>'
             '<linearGradient id="gLine" x1="0" x2="1" y1="0" y2="0"><stop offset="0" stop-color="#f59e0b"/><stop offset="0.55" stop-color="#f43f5e"/><stop offset="1" stop-color="#d946ef"/></linearGradient>'
             '<linearGradient id="gArea" x1="0" x2="0" y1="0" y2="1"><stop offset="0" stop-color="#f43f5e" stop-opacity="0.34"/><stop offset="1" stop-color="#f43f5e" stop-opacity="0"/></linearGradient>'
             '<linearGradient id="gBar" x1="0" x2="0" y1="0" y2="1"><stop offset="0" stop-color="#a78bfa" stop-opacity="0.95"/><stop offset="1" stop-color="#6366f1" stop-opacity="0.55"/></linearGradient>'
             '<linearGradient id="gRain" x1="0" x2="0" y1="0" y2="1"><stop offset="0" stop-color="#22d3ee" stop-opacity="0.95"/><stop offset="1" stop-color="#38bdf8" stop-opacity="0.35"/></linearGradient>'
             '</defs>']
    for frac in (0, 0.25, 0.5, 0.75, 1):  # grid + both axes' labels
        y = mt + ph * (1 - frac)
        parts.append(f'<line x1="{ml}" x2="{ml + pw}" y1="{y:.1f}" y2="{y:.1f}" class="grid"/>')
        parts.append(f'<text x="{ml - 8}" y="{y + 4:.1f}" class="tick" text-anchor="end">{lmax * frac:.0f}</text>')
        if has_bars:
            parts.append(f'<text x="{ml + pw + 8}" y="{y + 4:.1f}" class="tick tick-r">{bmax * frac:.0f}</text>')
    if threshold:
        ty = base - _scale(threshold, 0, lmax, 0, ph)
        parts.append(f'<line x1="{ml}" x2="{ml + pw}" y1="{ty:.1f}" y2="{ty:.1f}" class="std"/>')
        parts.append(f'<text x="{ml + 6}" y="{ty - 5:.1f}" class="std-label">{threshold:.0f} = India 24 h limit</text>')
    for i, v in enumerate(bars):
        if v is None:
            continue
        h = _scale(v, 0, bmax, 0, ph)
        parts.append(f'<rect x="{x(i) - step * 0.35:.1f}" y="{base - h:.1f}" width="{step * 0.7:.1f}" height="{h:.1f}" rx="3" class="bar"><title>{html.escape(dates[i])}: {v:.0f}</title></rect>')
    if rain:
        rvals = [v for v in rain if v is not None and v > 0]
        rmax = max(rvals) if rvals else 1
        for i, v in enumerate(rain):
            if v is None or v <= 0:
                continue
            h = max(5.0, _scale(v, 0, rmax, 0, ph * 0.33))
            w = step * 0.44
            parts.append(f'<path d="M{x(i) - w / 2:.1f},{mt:.1f} h{w:.1f} v{h - 3:.1f} q0,3 -3,3 h-{max(w - 6, 0):.1f} q-3,0 -3,-3 z" class="rain"><title>{html.escape(dates[i])}: {v:.1f} mm rain</title></path>')
    seg, path = [], []
    for i, v in enumerate(line):
        if v is None:
            if seg:
                path.append(seg)
                seg = []
            continue
        seg.append((x(i), base - _scale(v, 0, lmax, 0, ph), dates[i], v))
    if seg:
        path.append(seg)
    for s in path:
        if len(s) > 1:
            pts = " ".join(f"{px:.1f},{py:.1f}" for px, py, _, _ in s)
            parts.append(f'<path class="area" d="M{s[0][0]:.1f},{base:.1f} L{pts.replace(" ", " L")} L{s[-1][0]:.1f},{base:.1f} Z"/>')
            parts.append(f'<polyline class="line" points="{pts}"/>')
        for px, py, d, v in s:
            parts.append(f'<circle cx="{px:.1f}" cy="{py:.1f}" r="3.4" class="dot"><title>{html.escape(d)}: {v:.1f}</title></circle>')
    every = max(1, n // 8)
    for i in range(0, n, every):
        parts.append(f'<text x="{x(i):.1f}" y="{base + 18}" class="tick" text-anchor="middle">{html.escape(tick_fmt(dates[i]))}</text>')
    parts.append("</svg>")
    return "".join(parts)


# ---------------------------------------------------------------- HTML pieces


def _read(name: str, proc_dir: Path = PROC_DIR) -> pd.DataFrame | None:
    p = proc_dir / name
    return pd.read_csv(p) if p.exists() else None


LABEL = {"ok": "OK", "gaps": "GAPS", "stale": "STALE", "down": "DOWN", "none": "NO DATA"}


def health_html(rows: list[dict], today: date | None = None) -> str:
    today = today or date.today()
    out = ['<div class="sources">']
    for r in rows:
        latest = r["latest"].strftime("%d %b %Y") if r["latest"] else "-"
        age = "no files" if r["age_days"] is None else f'{r["age_days"]} d old &middot; normal lag {r["lag"]} d'
        miss = len(r["holes"])
        miss_txt = f'<span class="miss-n">{miss} missing day{"s" if miss != 1 else ""}</span>' if miss else '<span class="ok-n">no missing days</span>'
        out.append(
            f'<div class="src"><div class="src-top"><div><div class="src-name">{html.escape(r["name"])}</div>'
            f'<div class="muted">{html.escape(r["meaning"])}</div></div>'
            f'<span class="chip {r["status"]}">{LABEL[r["status"]]}</span></div>'
            f'<div class="src-meta"><span><b>{latest}</b> newest</span><span>{age}</span>{miss_txt}</div>'
            f'{day_strip(r.get("dates", []), today)}</div>')
    out.append("</div>")
    out.append('<div class="legend"><span><i class="cell ok"></i>file present</span><span><i class="cell miss"></i>missing</span>'
               '<span><i class="cell pend"></i>not yet expected</span><span><i class="cell pre"></i>before first file</span>'
               '<span class="muted">last 30 days, oldest on the left</span></div>')
    return "".join(out)


def stations_html(health: pd.DataFrame | None) -> str:
    if health is None or health.empty:
        return '<p class="muted">Station-health results not available.</p>'
    shown = health[health["level"].isin(["flag", "watch"])]
    if shown.empty:
        return "<p>No stations flagged or on watch.</p>"
    out = ['<div class="cards">']
    for _, r in shown.iterrows():
        level = html.escape(str(r["level"]))
        out.append(f'<div class="scard {level}"><div class="scard-top"><span class="sname">{html.escape(str(r["station"]))}</span>'
                   f'<span class="chip {level}">{level.upper()}</span></div><p>{html.escape(str(r["reason"]))}</p></div>')
    out.append("</div>")
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
    median = float(df["network_median_rho"].iloc[0])
    pct = lambda r: max(0.0, min(100.0, (float(r) + 1) / 2 * 100))  # noqa: E731
    out = [head, '<div class="gauges">',
           '<div class="g-head"><span>-1 moves opposite the satellite</span><span>0</span><span>+1 moves with it</span></div>']
    for _, r in pd.concat([flagged, watch]).drop_duplicates("station").iterrows():
        out.append(
            f'<div class="g-row"><div class="g-name">{html.escape(str(r["station"]))}<div class="muted">{int(r["n_days"])} days &middot; q {r["q_value"]:.2f}</div></div>'
            f'<div class="g-track"><span class="g-med" style="left:{pct(median):.1f}%" title="network median {median:.2f}"></span>'
            f'<span class="g-dot" style="left:{pct(r["rho"]):.1f}%"></span></div>'
            f'<div class="g-val">{r["rho"]:+.2f}<div class="muted">median {median:.2f}</div></div></div>')
    out.append("</div>")
    return "".join(out)


def stubble_html(daily: pd.DataFrame | None) -> str:
    if daily is None or daily.empty or "pm25_delhi" not in daily:
        return '<p class="muted">Attribution results not available.</p>'
    col = next((c for c in EXPOSURE_COLUMNS if c in daily.columns), None)
    d = daily.dropna(subset=["pm25_delhi"]).sort_values("date")
    if col is None or d.empty:
        return '<p class="muted">Attribution results not available.</p>'
    rain = None
    if "precip_delhi_mm" in d.columns and d["precip_delhi_mm"].notna().any():
        rain = [None if pd.isna(v) else float(v) for v in d["precip_delhi_mm"]]
    svg = line_bar_svg(list(d["date"]), [float(v) for v in d["pm25_delhi"]], [None if pd.isna(v) else float(v) for v in d[col]],
                       "Delhi PM2.5 (ug/m3)", "Fire exposure index", rain=rain, threshold=PM25_STANDARD)
    legend = ('<div class="legend chart-legend"><span><i class="sw line-sw"></i>Delhi PM2.5, daily mean (left axis, &micro;g/m&sup3;)</span>'
              '<span><i class="sw bar-sw"></i>Fire exposure index (right axis)</span>'
              + ('<span><i class="sw rain-sw"></i>Rain, hanging from the top (mm)</span>' if rain else "") + "</div>")
    return legend + svg


def hero_stats(today: date, raw_dir: Path = RAW_DIR, proc_dir: Path = PROC_DIR, health: list[dict] | None = None) -> list[tuple[str, str, str]]:
    """Four headline numbers: (value, label, sub). Every one degrades to a dash if its data is missing."""
    health = health if health is not None else all_source_health(today, raw_dir)
    ok = sum(1 for r in health if r["status"] == "ok")
    stats = [(f"{ok}/{len(health)}", "sources healthy", "OK = fresh and no missing days")]

    stations = "-"
    feeds = sorted(glob.glob(str(raw_dir / "cpcb_ncr_*.csv")))
    if feeds:
        try:
            stations = str(pd.read_csv(feeds[-1], usecols=["station"])["station"].nunique())
        except Exception:
            pass
    stats.append((stations, "stations in the feed", "newest CPCB snapshot"))

    pm, pm_sub = "-", "no data"
    daily = _read("stubble_transport_daily.csv", proc_dir)
    if daily is not None and "pm25_delhi" in daily and daily["pm25_delhi"].notna().any():
        last = daily.dropna(subset=["pm25_delhi"]).sort_values("date").iloc[-1]
        pm, pm_sub = f'{last["pm25_delhi"]:.0f}', f'&micro;g/m&sup3; &middot; Delhi mean, {pd.Timestamp(last["date"]):%d %b}'
    stats.append((pm, "Delhi PM2.5", pm_sub))

    flagged, sub = "-", "station health not available"
    h = _read("neighbor_station_health.csv", proc_dir)
    if h is not None and "level" in h:
        flagged = str(int((h["level"] == "flag").sum()))
        sub = f'{int((h["level"] == "watch").sum())} on watch &middot; of {len(h)} stations'
    stats.append((flagged, "stations flagged", sub))
    return stats


CSS = """:root{--bg1:#101436;--bg2:#070a1c;--bg3:#010103;
--card:linear-gradient(145deg,rgba(255,255,255,.085),rgba(255,255,255,.025));--fg:#e6eaf7;--muted:#98a3c2;--line:rgba(255,255,255,.12);
--shadow:0 18px 44px -18px rgba(0,0,0,.75),inset 0 1px 0 rgba(255,255,255,.14),inset 0 0 0 1px rgba(255,255,255,.03);
--tile:rgba(255,255,255,.05);--tile-line:rgba(255,255,255,.2);--cell-pre:rgba(255,255,255,.08);--grid:rgba(255,255,255,.09)}
*{box-sizing:border-box}
html{background:var(--bg1)}
body{margin:0;color:var(--fg);font:15px/1.55 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;min-height:100vh;
background:linear-gradient(180deg,var(--bg1) 0%,var(--bg2) 45%,var(--bg3) 100%);background-attachment:fixed}
main{max-width:1000px;margin:0 auto;padding:22px 16px 56px}
.hero{position:relative;overflow:hidden;border-radius:26px;padding:34px 30px 26px;color:#fff;
background:linear-gradient(180deg,rgba(40,38,110,.55) 0%,rgba(14,16,44,.6) 100%);border:1px solid rgba(255,255,255,.18);box-shadow:0 26px 60px -26px rgba(0,0,0,.9),inset 0 1px 0 rgba(255,255,255,.25);backdrop-filter:blur(18px);-webkit-backdrop-filter:blur(18px)}
.hero h1{margin:0;font-size:clamp(26px,4.6vw,38px);line-height:1.1;letter-spacing:-.025em;font-weight:800;position:relative;z-index:1}
.hero .sub{margin:8px 0 0;max-width:none;opacity:.9;font-size:15px;position:relative;z-index:1}
.hero .stamp{margin-top:6px;font-size:12px;opacity:.7;position:relative;z-index:1}
.tiles{display:grid;grid-template-columns:repeat(4,1fr);gap:12px;margin-top:22px;position:relative;z-index:1}
.tile{box-shadow:inset 0 1px 0 rgba(255,255,255,.25);background:var(--tile);border:1px solid var(--tile-line);border-radius:18px;padding:14px 16px;backdrop-filter:blur(10px);-webkit-backdrop-filter:blur(10px)}
.tile .v{font-size:clamp(26px,4vw,34px);font-weight:800;letter-spacing:-.02em;line-height:1.1}
.tile .l{font-size:13px;font-weight:600;margin-top:2px}.tile .s{font-size:12px;opacity:.78;margin-top:2px}
.banner{margin:16px 0 0;padding:11px 16px;border-radius:14px;font-size:14px;background:linear-gradient(90deg,rgba(245,158,11,.22),rgba(251,191,36,.10));border:1px solid rgba(245,158,11,.45)}
.card{margin-top:22px;background:var(--card);border:1px solid var(--line);border-radius:22px;padding:22px 22px 18px;box-shadow:var(--shadow);backdrop-filter:blur(22px) saturate(140%);-webkit-backdrop-filter:blur(22px) saturate(140%)}
.card h2{margin:0 0 14px;font-size:19px;letter-spacing:-.015em;display:flex;align-items:center;gap:11px}
.num{display:inline-grid;place-items:center;width:30px;height:30px;border-radius:10px;font-size:14px;font-weight:800;color:#fff;background:linear-gradient(135deg,#6366f1,#06b6d4);box-shadow:0 6px 14px -6px rgba(99,102,241,.8)}
.muted{color:var(--muted);font-size:13px}
.note{margin-top:14px;padding:12px 15px;border-radius:14px;font-size:14px;background:linear-gradient(135deg,rgba(99,102,241,.10),rgba(6,182,212,.07));border:1px solid var(--line);border-left:4px solid #8b5cf6}
.note.warn{border-left-color:#f59e0b;background:linear-gradient(135deg,rgba(245,158,11,.12),rgba(244,63,94,.06))}
.chip{display:inline-block;padding:3px 11px;border-radius:999px;font-size:12px;font-weight:800;letter-spacing:.04em;white-space:nowrap}
.chip.ok{background:linear-gradient(135deg,#059669,#34d399);color:#02170c}
.chip.gaps,.chip.watch{background:linear-gradient(135deg,#f59e0b,#fcd34d);color:#2b1700}
.chip.stale{background:linear-gradient(135deg,#f97316,#fdba74);color:#2b0e00}
.chip.down,.chip.flag,.chip.none{background:linear-gradient(135deg,#e11d48,#fb7185);color:#fff}
.sources{display:grid;gap:12px}
.src{border:1px solid var(--line);border-radius:16px;padding:14px 16px;background:linear-gradient(145deg,rgba(255,255,255,.07),rgba(255,255,255,.015));box-shadow:inset 0 1px 0 rgba(255,255,255,.1)}
.src-top{display:flex;justify-content:space-between;align-items:flex-start;gap:12px}
.src-name{font-weight:700;font-size:16px}
.src-meta{display:flex;flex-wrap:wrap;gap:6px 18px;margin:8px 0 10px;font-size:13px;color:var(--muted)}.src-meta b{color:var(--fg)}
.miss-n{color:#f43f5e;font-weight:700}.ok-n{color:#10b981;font-weight:700}
.strip{display:grid;grid-template-columns:repeat(30,1fr);gap:3px}
.cell{display:block;height:16px;border-radius:5px}
.cell.ok{background:linear-gradient(180deg,#4ade80,#10b981)}.cell.miss{background:linear-gradient(180deg,#fb7185,#e11d48)}
.cell.pre{background:var(--cell-pre)}.cell.pend{border:1.5px dashed var(--muted);opacity:.55}
.legend{display:flex;flex-wrap:wrap;gap:6px 16px;margin-top:12px;font-size:12.5px;color:var(--muted);align-items:center}
.legend span{display:inline-flex;align-items:center;gap:6px}.legend i.cell{width:14px;height:14px;display:inline-block}
.chart-legend{margin:0 0 6px}
.sw{display:inline-block;width:22px;height:8px;border-radius:5px}
.line-sw{background:linear-gradient(90deg,#f59e0b,#f43f5e,#d946ef)}.bar-sw{background:linear-gradient(180deg,#a78bfa,#6366f1)}.rain-sw{background:linear-gradient(180deg,#22d3ee,#38bdf8)}
.chart{width:100%;height:auto;display:block}
.chart .grid{stroke:var(--grid);stroke-width:1}.chart .tick{fill:var(--muted);font-size:11px}.chart .tick-r{fill:#a99cf5}
.chart .bar{fill:url(#gBar)}.chart .rain{fill:url(#gRain)}.chart .area{fill:url(#gArea);stroke:none}
.chart .line{fill:none;stroke:url(#gLine);stroke-width:3;stroke-linejoin:round;stroke-linecap:round;filter:drop-shadow(0 4px 6px rgba(244,63,94,.35))}
.chart .dot{fill:#fff;stroke:#f43f5e;stroke-width:2}
.chart .std{stroke:#f59e0b;stroke-width:1.4;stroke-dasharray:6 5}.chart .std-label{fill:#d97706;font-size:11px;font-weight:600}
.cards{display:grid;grid-template-columns:repeat(auto-fill,minmax(270px,1fr));gap:12px}
.scard{border:1px solid var(--line);border-radius:16px;padding:13px 15px;border-left:5px solid #f59e0b;background:linear-gradient(135deg,rgba(245,158,11,.08),transparent)}
.scard.flag{border-left-color:#e11d48;background:linear-gradient(135deg,rgba(225,29,72,.10),transparent)}
.scard-top{display:flex;justify-content:space-between;align-items:center;gap:10px}.sname{font-weight:700}
.scard p{margin:8px 0 0;font-size:13.5px;color:var(--muted)}
.gauges{margin-top:10px;display:grid;gap:10px}
.g-head{display:grid;grid-template-columns:1fr 1fr 1fr;font-size:11.5px;color:var(--muted)}.g-head span:nth-child(2){text-align:center}.g-head span:nth-child(3){text-align:right}
.g-row{display:grid;grid-template-columns:230px 1fr 90px;gap:14px;align-items:center}
.g-name{font-weight:600;font-size:14px}.g-val{text-align:right;font-weight:800;font-size:16px}
.g-track{position:relative;height:12px;border-radius:999px;background:linear-gradient(90deg,#e11d48,#f59e0b 40%,#34d399 100%);box-shadow:inset 0 1px 3px rgba(0,0,0,.25)}
.g-dot{position:absolute;top:50%;width:20px;height:20px;border-radius:50%;background:#fff;border:3px solid #1e1b4b;transform:translate(-50%,-50%);box-shadow:0 3px 8px rgba(0,0,0,.4)}
.g-med{position:absolute;top:-5px;bottom:-5px;width:3px;border-radius:2px;background:var(--fg);opacity:.85;transform:translateX(-50%)}

.nav{display:flex;flex-wrap:wrap;gap:8px;margin:16px 0 0}
.nav a{color:var(--fg);text-decoration:none;font-size:13px;font-weight:600;padding:6px 13px;border-radius:999px;background:linear-gradient(145deg,rgba(255,255,255,.09),rgba(255,255,255,.03));border:1px solid var(--line);box-shadow:inset 0 1px 0 rgba(255,255,255,.12)}
.nav a:hover{border-color:rgba(255,255,255,.35)}
html{scroll-behavior:smooth}.card{scroll-margin-top:14px}
.split{display:grid;grid-template-columns:1.15fr 1fr;gap:22px;align-items:start}
.sub-h{margin:20px 0 8px;font-size:14px;font-weight:700;color:var(--muted);letter-spacing:.04em;text-transform:uppercase}
.sub-h:first-child{margin-top:0}
.map{width:100%;height:auto;display:block}.map-bg{fill:rgba(255,255,255,.035);stroke:var(--line)}
.dotc{display:inline-block;width:11px;height:11px;border-radius:50%}.dotc.ring{background:transparent;border:2.5px solid}.flagc{border-color:#f43f5e}.watchc{border-color:#fbbf24}
.cities{display:grid;grid-template-columns:repeat(auto-fill,minmax(150px,1fr));gap:10px}
.city{border:1px solid var(--line);border-left:4px solid var(--c);border-radius:14px;padding:10px 13px;background:linear-gradient(145deg,rgba(255,255,255,.07),rgba(255,255,255,.015))}
.city-n{font-weight:700}.city-v{font-size:30px;font-weight:800;line-height:1.15;color:var(--c)}
.rank{display:grid;gap:5px}.rank-row{display:grid;grid-template-columns:170px 1fr 38px;gap:10px;align-items:center;font-size:13px}
.rank-n{white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.rank-v{text-align:right;font-weight:700}
.rank-t{position:relative;height:11px;border-radius:6px;background:rgba(255,255,255,.07)}.rank-b{display:block;height:100%;border-radius:6px}
.rank-lim{position:absolute;top:-3px;bottom:-3px;width:2px;background:#fff;opacity:.85}.rank-gap{text-align:center;font-size:12px}
.heat{width:100%;height:auto;display:block}.heat .hole{fill:rgba(255,255,255,.05)}.heat .tick{font-size:10.5px}
.ramp{display:flex;align-items:center;gap:10px;margin-top:10px;font-size:12px;color:var(--muted)}.ramp i{flex:0 0 220px;height:10px;border-radius:6px}
.minis{display:grid;grid-template-columns:repeat(auto-fill,minmax(220px,1fr));gap:12px}
.mini{border:1px solid var(--line);border-radius:16px;padding:12px 14px;background:linear-gradient(145deg,rgba(255,255,255,.07),rgba(255,255,255,.015))}
.mini-h{display:flex;justify-content:space-between;align-items:baseline;gap:8px}.mini-t{font-size:12.5px;font-weight:600;color:var(--muted)}
.mini-v{font-weight:800;font-size:18px}.mini-v small{font-size:11px;font-weight:600;color:var(--muted)}
.spark{width:100%;height:auto;display:block;margin-top:4px}.mini-r{display:flex;justify-content:space-between;font-size:11px}
.meter{padding:14px 16px;border:1px solid var(--line);border-radius:16px;background:linear-gradient(145deg,rgba(255,255,255,.07),rgba(255,255,255,.015))}
.meter-top{margin-bottom:9px;font-size:15px}.meter-track{height:14px;border-radius:999px;background:rgba(255,255,255,.08);overflow:hidden;box-shadow:inset 0 1px 3px rgba(0,0,0,.5)}
.meter-fill{display:block;height:100%;border-radius:999px;background:linear-gradient(90deg,#6366f1,#22d3ee)}.meter .muted{margin-top:8px}
.count{display:grid;grid-template-columns:repeat(auto-fit,minmax(280px,1fr));gap:12px}
.count-i{display:flex;gap:14px;align-items:center;border:1px solid var(--line);border-radius:16px;padding:12px 16px;background:linear-gradient(145deg,rgba(255,255,255,.07),rgba(255,255,255,.015))}
.count-n{font-size:30px;font-weight:800;min-width:78px;background:linear-gradient(135deg,#a5b4fc,#67e8f9);-webkit-background-clip:text;background-clip:text;color:transparent}
.stats{display:flex;flex-wrap:wrap;gap:10px 28px;margin:12px 0 4px}.stat{display:flex;flex-direction:column}.stat-v{font-size:26px;font-weight:800;line-height:1.1}
.sc{fill:#a5b4fc;fill-opacity:.8;stroke:#fff;stroke-width:1.2}
.runs{display:grid;gap:7px}.run{display:flex;flex-wrap:wrap;align-items:center;gap:6px 14px;border:1px solid var(--line);border-radius:12px;padding:8px 14px;background:rgba(255,255,255,.03);font-size:14px}
details.how{margin-top:14px;border:1px solid var(--line);border-radius:14px;padding:10px 15px;background:rgba(255,255,255,.03);font-size:14px}
details.how summary{cursor:pointer;font-weight:700}details.how p{margin:.5em 0 0;color:var(--muted)}
.foot{margin-top:30px;text-align:center;font-size:12.5px;color:var(--muted)}

/* ---- calm layer: muted deep-teal palette, refined type, quiet glass (overrides everything above) ---- */
:root{--bg1:#0c171b;--bg2:#0a1317;--bg3:#070d10;--fg:#e8eeed;--muted:#8ea3a5;--line:rgba(255,255,255,.085);--edge:rgba(255,255,255,.095);
--teal:#6fbfb2;--sage:#a3d2b8;--amber:#dfb277;--coral:#d9897c;--ink:#071013;--grid:rgba(255,255,255,.06);
--glass:linear-gradient(180deg,rgba(255,255,255,.055),rgba(255,255,255,.025));--glass-in:linear-gradient(180deg,rgba(255,255,255,.05),rgba(255,255,255,.02));
--sans:"Inter","Segoe UI Variable Text","Segoe UI Variable","Segoe UI",system-ui,-apple-system,"Helvetica Neue",Arial,sans-serif;
--serif:"Iowan Old Style","Palatino Linotype","Book Antiqua",Palatino,Georgia,serif;--mono:var(--sans)}
html{background:var(--bg1)}
body{font:15px/1.65 var(--sans);font-feature-settings:"tnum","cv11","ss01";-webkit-font-smoothing:antialiased;color:var(--fg);
background:radial-gradient(1100px 520px at 50% -140px,rgba(111,191,178,.10),transparent 70%),linear-gradient(180deg,var(--bg1) 0%,var(--bg2) 55%,var(--bg3) 100%);background-attachment:fixed}
main{max-width:1040px}
.card,.hero{border-radius:20px;border:1px solid var(--edge);background:var(--glass);box-shadow:0 24px 50px -32px rgba(0,0,0,.8),inset 0 1px 0 rgba(255,255,255,.07);backdrop-filter:blur(16px) saturate(120%);-webkit-backdrop-filter:blur(16px) saturate(120%)}
.card{padding:26px 26px 20px;margin-top:20px}
.card:before,.card:after{display:none}
.card h2{font:600 13px/1.2 var(--sans);letter-spacing:.14em;text-transform:uppercase;color:var(--sage);margin:0 0 18px;gap:10px}
.card h2:before{width:6px;height:6px;border-radius:50%;background:var(--teal);box-shadow:none}
.hero{padding:44px 38px 30px;background:linear-gradient(180deg,rgba(111,191,178,.075),rgba(255,255,255,.02))}
.hero h1{font:400 clamp(34px,5.6vw,54px)/1.14 var(--serif);letter-spacing:-.012em;padding-bottom:.06em;background:none;-webkit-text-fill-color:currentColor;color:#f1f6f4;filter:none}
.hero h1:before{content:none}
.brand{display:flex;align-items:center;gap:14px;margin-bottom:26px}
.logo{width:46px;height:46px;flex:0 0 auto;display:block}
.brand-n{font:700 15px/1 var(--sans);letter-spacing:.3em;color:var(--sage)}
.hero .sub{font-size:16px;line-height:1.6;color:#b9c9c9;opacity:1;max-width:56ch}
.hero .stamp{color:var(--muted);opacity:1;letter-spacing:.02em}
.tiles{gap:14px;margin-top:30px}
.tile,.src,.mini,.meter,.count-i,.city,.run,.scard,details.how,.rank-t{background:var(--glass-in);border:1px solid var(--edge);border-top-color:var(--edge);box-shadow:none;backdrop-filter:none;-webkit-backdrop-filter:none}
.tile{border-radius:16px;padding:16px 18px}
.tile .v,.stat-v,.city-v,.mini-v,.rank-v,.g-val,.count-n,.meter-top b{font-family:var(--sans);font-weight:600;letter-spacing:-.01em;font-variant-numeric:tabular-nums}
.tile .v{font-size:clamp(26px,3.6vw,32px);color:#f1f6f4}
.tile .l{font:600 11px/1.3 var(--sans);letter-spacing:.12em;text-transform:uppercase;color:var(--sage);margin-top:6px}
.tile .s{color:var(--muted);opacity:1}
.banner{background:rgba(223,178,119,.09);border:1px solid rgba(223,178,119,.32);color:#ecd7b8;border-radius:14px}
.note{background:rgba(111,191,178,.06);border:1px solid var(--edge);border-left:2px solid var(--teal);box-shadow:none;color:#cfdcda}
.note.warn{background:rgba(223,178,119,.07);border-left-color:var(--amber)}
.chip{font:600 10.5px/1 var(--sans);letter-spacing:.1em;text-transform:uppercase;padding:5px 10px;box-shadow:none}
.chip.ok{background:rgba(111,191,178,.16);color:var(--sage);border:1px solid rgba(111,191,178,.35)}
.chip.gaps,.chip.watch{background:rgba(223,178,119,.14);color:var(--amber);border:1px solid rgba(223,178,119,.35)}
.chip.stale{background:rgba(217,160,124,.14);color:#e0a888;border:1px solid rgba(217,160,124,.35)}
.chip.down,.chip.flag,.chip.none{background:rgba(217,137,124,.14);color:var(--coral);border:1px solid rgba(217,137,124,.38)}
.sub-h{font:600 11px/1.2 var(--sans);letter-spacing:.16em;color:var(--muted);opacity:1;text-transform:uppercase}.sub-h:before{content:none}
.src-name,.sname,.city-n{font-weight:600}
.miss-n{color:var(--coral)}.ok-n{color:var(--teal)}
.cell{border-radius:4px}.cell.ok{background:rgba(111,191,178,.78);box-shadow:none}.cell.miss{background:rgba(217,137,124,.82)}
.nav{gap:8px;margin-top:18px}
.nav a{font:500 12px/1 var(--sans);letter-spacing:.06em;text-transform:none;padding:9px 15px;background:var(--glass-in);border:1px solid var(--edge);box-shadow:none;color:#cfdcda}
.nav a:hover{border-color:rgba(111,191,178,.55);color:var(--sage)}
.line-sw{background:var(--amber)}.bar-sw{background:rgba(111,191,178,.6)}.rain-sw{background:#7fb2d6}
.chart .tick-r{fill:#9bb8d0}
.chart .line{stroke:var(--amber);stroke-width:2.4;filter:none}.chart .dot{fill:var(--ink);stroke:var(--amber);stroke-width:2}
.chart .bar{fill:rgba(111,191,178,.42)}.chart .rain{fill:rgba(127,178,214,.55)}.chart .area{fill:rgba(223,178,119,.08)}
.chart .std{stroke:var(--coral);stroke-width:1.2;stroke-dasharray:5 6}.chart .std-label{fill:var(--coral)}
.scard{border-left:3px solid var(--amber);background:rgba(223,178,119,.045)}.scard.flag{border-left-color:var(--coral);background:rgba(217,137,124,.055)}
.g-track{height:6px;background:linear-gradient(90deg,var(--coral),var(--amber) 45%,var(--teal));box-shadow:none}
.g-dot{width:16px;height:16px;border:3px solid var(--ink);background:#f1f6f4;box-shadow:0 0 0 1px rgba(255,255,255,.3)}
.meter-track{height:8px;background:rgba(255,255,255,.07);box-shadow:none}.meter-fill{background:linear-gradient(90deg,var(--teal),var(--sage))}
.count-n{background:none;-webkit-text-fill-color:currentColor;color:var(--sage)}
.rank-t{height:8px;border:0}.rank-b{opacity:.9}.rank-lim{background:var(--fg);opacity:.5}
.city{border-left-width:3px}.city-v{font-weight:600}
.sc{fill:var(--teal);fill-opacity:.7;stroke:var(--ink)}
.map-bg{fill:rgba(111,191,178,.03);stroke:var(--edge)}.map-lbl{fill:#d6e2e0;stroke:#0a1317;stroke-width:3px}
.heat .tick,.map .tick{fill:#b3c3c3}.heat .hole{fill:rgba(255,255,255,.04)}
.foot{font:500 12px/1.6 var(--sans);letter-spacing:.04em;color:var(--muted)}
.foot a{color:var(--sage);border-bottom:1px solid rgba(163,210,184,.4);font-weight:600}.foot a:hover{border-bottom-color:var(--sage)}
.map .map-lbl{fill:#e8f0ee;paint-order:stroke fill;stroke:#0b1519;stroke-width:3.5px;stroke-linejoin:round;font-size:11px;font-weight:500}
.cities{grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:12px}
.city{display:flex;flex-direction:column;gap:2px;padding:14px 16px;border-radius:16px}
.city-n{font:600 11px/1.3 var(--sans);letter-spacing:.12em;text-transform:uppercase;color:var(--muted)}
.city-v{font-size:38px;line-height:1.1;margin:4px 0 2px}
.city .muted{font-size:12.5px;line-height:1.45}
.count{grid-template-columns:repeat(auto-fit,minmax(300px,1fr))}
.count-i{gap:20px;align-items:center}
.count-n{flex:0 0 auto;min-width:0;font-size:30px;line-height:1.1;white-space:nowrap}
.foot{margin-top:22px;padding:26px 26px 18px;text-align:left;border:1px solid var(--edge);border-radius:20px;background:var(--glass);font:400 13px/1.6 var(--sans);letter-spacing:0}
.foot-grid{display:grid;grid-template-columns:1.1fr 1.3fr 1.2fr;gap:28px}
.foot h3{margin:0 0 10px;font:600 11px/1.2 var(--sans);letter-spacing:.16em;text-transform:uppercase;color:var(--sage)}
.foot p{margin:0;color:#b9c9c9}
.foot ul{list-style:none;margin:0;padding:0;display:grid;gap:8px}
.foot li{display:flex;flex-direction:column;align-items:flex-start;gap:1px}.foot li span{color:var(--muted);font-size:12.5px}
.foot-bar{display:flex;flex-wrap:wrap;gap:8px 22px;align-items:baseline;margin-top:22px;padding-top:14px;border-top:1px solid var(--edge);color:var(--muted);font-size:12.5px}
.foot-bar b{color:var(--fg);font-weight:600}.foot-bar a:first-of-type{margin-left:auto}
@media (max-width:760px){.foot{padding:20px 16px 14px}.foot-grid{grid-template-columns:1fr;gap:22px}.foot-bar a:first-of-type{margin-left:0}}
@media (max-width:760px){.hero{padding:30px 22px 22px}.card{padding:20px 16px 14px}}
.tabs{margin-top:18px}
.tabin{position:absolute;opacity:0;pointer-events:none}
.tabbar{display:flex;gap:6px;overflow-x:auto;padding:6px;border:1px solid var(--edge);border-radius:18px;background:rgba(14,26,30,.82);position:sticky;top:8px;z-index:5;backdrop-filter:blur(14px);-webkit-backdrop-filter:blur(14px);scrollbar-width:none}
.tabbar::-webkit-scrollbar{display:none}
.tabbar label{flex:1 0 auto;display:flex;align-items:center;gap:9px;padding:10px 13px;border-radius:13px;cursor:pointer;color:var(--muted);border:1px solid transparent;white-space:nowrap;transition:background .15s,color .15s}
.tabbar label:hover{color:var(--fg);background:rgba(255,255,255,.04)}
.tabbar svg{width:18px;height:18px;flex:0 0 auto;fill:none;stroke:var(--c);stroke-width:1.8;stroke-linecap:round;stroke-linejoin:round;opacity:.7}
.tabbar b{display:block;font:600 13px/1.2 var(--sans)}.tabbar small{display:block;font:500 11px/1.3 var(--sans);opacity:.7}
.panel{display:none}.panel>.card:first-child{margin-top:14px}
@media (max-width:760px){.tabbar{top:4px}.tabbar label{padding:9px 12px}.tabbar small{display:none}}
.tbl{width:100%;border-collapse:collapse;font-size:13px;min-width:520px}
.tbl th{text-align:left;font:600 10.5px/1.3 var(--sans);letter-spacing:.12em;text-transform:uppercase;color:var(--muted);padding:6px 10px 8px;border-bottom:1px solid var(--edge)}
.tbl td{padding:8px 10px;border-bottom:1px solid rgba(255,255,255,.05)}.tbl tr:last-child td{border-bottom:0}
.tbl .n{text-align:right;font-variant-numeric:tabular-nums}.tbl td .muted{font-size:11.5px}
.scroll{overflow-x:auto}
@media (max-width:760px){.split{grid-template-columns:1fr}.rank-row{grid-template-columns:120px 1fr 34px}.tiles{grid-template-columns:repeat(2,1fr)}.hero{padding:26px 20px 20px}.card{padding:18px 16px 14px}
.g-row{grid-template-columns:1fr 70px;gap:6px 12px}.g-track{grid-column:1 / -1;order:3}.g-head{display:none}}
"""


def history_html(proc_dir: Path = PROC_DIR) -> tuple[str, str]:
    """The 2015-2025 panels: (network audit, seasons). Empty strings when the backfill has not been run here."""
    master = _read("historical/station_master.csv", proc_dir)
    audit = _read("historical/station_year_audit.csv", proc_dir)
    league = _read("historical/city_league.csv", proc_dir)
    qc = _read("historical/silver_qc_summary.csv", proc_dir)
    comp = _read("historical/completeness_state_month.csv", proc_dir)
    delhi = _read("historical/delhi_pm25_daily.csv", proc_dir)
    fires = _read("historical/fire_daily.csv", proc_dir)
    if all(x is None for x in (master, audit, league, qc, comp, delhi, fires)):
        return "", ""  # the backfill has not been run on this machine: show nothing rather than a page of "not available"
    local = proc_dir.parent / "historical"
    count = lambda sub, pat: len(list((local / sub).glob(pat))) if (local / sub).exists() else 0  # noqa: E731
    sources = [("CPCB station files, 15-minute", count("bronze/cpcb", "cpcb-air-quality-*.parquet"), 11, "One file per year, 2015 to 2025, from the CPCB data repository."),
               ("NASA FIRMS archive", count("firms", "firms_*.csv"), 19, "October and November, Suomi NPP from 2015 and NOAA-20 from 2018."),
               ("ERA5 history (winds, rain)", count("era5", "era5_*.nc"), 6, "Requested from Copernicus; needed before the stubble test can be repeated on past seasons.")]
    years = sorted(int(y) for y in audit["year"].unique()) if audit is not None and not audit.empty else []
    hy = years[-1] if years else 0
    season_years = sorted({int(str(d)[:4]) for d in delhi["date"]}) if delhi is not None and not delhi.empty else []
    n_audited = int(audit[audit["year"] == hy]["level"].isin(["ok", "watch", "flag"]).sum()) if hy else 0
    n_total = int((audit["year"] == hy).sum()) if hy else 0
    n_flag = int((audit[audit["year"] == hy]["level"] == "flag").sum()) if hy else 0
    india = f"""<section class="card" id="india"><h2>India, 2015 to 2025: the station network</h2>
<p class="muted" style="margin-top:-6px">A pan-India backfill of the CPCB station network, run through the same cleaning and neighbour checks as the live data.</p>
<div class="sub-h">Backfill health</div>{dh.backfill_html(qc, sources)}
<div class="note">Early years are thin: there were few continuous stations before 2018, so a season before then rests on 5 to 7 Delhi stations. Station counts are
shown on every table so that a quiet year is not mistaken for a clean one.</div>

<div class="sub-h">How much of each month was reported</div><div class="scroll">{dh.completeness_matrix_svg(comp)}</div>

<div class="sub-h">Station integrity across India, {hy}</div>
<div class="split"><div>{dh.india_map_svg(master, audit, hy)}</div>
<div><div class="sub-h">Cities with the most flagged hours, {hy}</div>{dh.league_html(league, hy)}</div></div>
<div class="note warn"><b>Read with care.</b> A flag means "have someone look", not "faulty": a station beside a road or kiln legitimately differs from its neighbours.
Only {n_audited} of the {n_total} stations reporting in {hy} could be checked at all, because the test compares each station with others within 25 km and many
stations have none. Of those {n_audited}, {n_flag} carry a flag, which needs just one stuck-sensor run or one sustained departure, so most flags are brief:
"hours flagged" is the share of a station&rsquo;s reported hours inside such an episode, and is a few percent even in the worst cities.</div>

<div class="sub-h">Stations that drifted against their neighbours</div>{dh.drift_watchlist_html(audit, master)}
<div class="note">Confirmed means the slope survived the multiple-testing correction and was large enough to matter. A station can drift because it was moved,
recalibrated or re-sited, which the data cannot tell apart from a failing sensor.</div>
</section>"""
    seasons = f"""<section class="card" id="seasons"><h2>Delhi, ten burning seasons</h2>
<div class="sub-h">Delhi in each burning season</div><div class="scroll">{dh.season_multiples_svg(delhi, fires, season_years)}</div>
<div class="note">Descriptive only. These panels do not control for rain or wind, so a season with more fires and higher PM2.5 shows nothing about cause.
Fire detections fall from about 100,000 a season around 2021 to under 15,000 in 2025 in this region.</div>

<div class="sub-h">Does the stubble signal repeat across seasons?</div>
<div class="note warn"><b>Not run yet.</b> The pre-registered test is applied to each season unchanged, once, and needs the 925 hPa winds and the rain record for those seasons.
Those come from Copernicus, whose queue has not accepted the requests so far. No result is shown here until it can be produced the same way as in 2026.</div>
</section>"""
    return india, seasons


TAB_DEFS = [  # (id, label, hint, accent, icon path, section keys)
    ("overview", "Overview", "Network now", "#6fbfb2", "M3 12h4l3-8 4 16 3-8h4", ["now"]),
    ("fires", "Fires", "and weather", "#dfb277", "M12 3c1 4 5 5 5 10a5 5 0 0 1-10 0c0-2 1-3 2-4 0 2 1 3 2 3 0-3-1-5 1-9z", ["fires"]),
    ("stations", "Stations", "Health, satellite", "#d9897c", "M12 3 4 6v6c0 5 3.5 8 8 9 4.5-1 8-4 8-9V6l-8-3zM9 12l2 2 4-4", ["health", "sat"]),
    ("data", "Data", "Flow, gaps", "#7fb2d6", "M4 19V5M4 19h16M8 15l3-4 3 2 5-6", ["flow", "quality", "heat"]),
    ("india", "India", "2015 to 2025", "#a3d2b8", "M9 4 3 6v14l6-2 6 2 6-2V4l-6 2-6-2zM9 4v14M15 6v14", ["india"]),
    ("seasons", "Seasons", "Ten years of Delhi", "#b7a6e0", "M3 17l5-6 4 3 5-8 4 5", ["seasons"]),
    ("pipeline", "Pipeline", "Runs, what is next", "#8ea3a5", "M4 8h13l-3-3M20 16H7l3 3", ["ops"]),
]


def tabs_css() -> str:
    """Rules for the CSS-only tabs: a hidden radio per tab selects its panel and highlights its label (no JavaScript)."""
    out = []
    for tid, *_ in TAB_DEFS:
        out.append(f"#t-{tid}:checked ~ #p-{tid}{{display:block}}")
        out.append(f"#t-{tid}:checked ~ .tabbar label[for=t-{tid}]{{background:color-mix(in srgb,var(--c) 15%,transparent);border-color:color-mix(in srgb,var(--c) 42%,transparent);color:var(--fg)}}")
        out.append(f"#t-{tid}:checked ~ .tabbar label[for=t-{tid}] svg{{opacity:1}}")
        out.append(f"#t-{tid}:focus-visible ~ .tabbar label[for=t-{tid}]{{outline:2px solid var(--c);outline-offset:2px}}")
    return "\n".join(out)


def tabs_html(sections: dict[str, str]) -> str:
    """The tab bar and its panels. A tab whose sections are all empty (for example the history before the backfill has run) is left out."""
    tabs = [(t, "".join(sections.get(k, "") for k in t[5])) for t in TAB_DEFS]
    tabs = [(t, body) for t, body in tabs if body.strip()]
    inputs = "".join(f'<input class="tabin" type="radio" name="tab" id="t-{t[0]}" aria-label="{html.escape(t[1])}"{" checked" if i == 0 else ""}>' for i, (t, _) in enumerate(tabs))
    labels = "".join(
        f'<label for="t-{t[0]}" style="--c:{t[3]}"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="{t[4]}"/></svg>'
        f'<span><b>{html.escape(t[1])}</b><small>{html.escape(t[2])}</small></span></label>' for t, _ in tabs)
    panels = "".join(f'<div class="panel" id="p-{t[0]}">{body}</div>' for t, body in tabs)
    return f'<div class="tabs">{inputs}<nav class="tabbar" aria-label="Dashboard sections">{labels}</nav>{panels}</div>'


def render(today: date, raw_dir: Path = RAW_DIR, proc_dir: Path = PROC_DIR) -> str:
    health = all_source_health(today, raw_dir)
    bad = [r for r in health if r["status"] in ("gaps", "stale", "down", "none")]
    banner = ("" if not bad else '<div class="banner"><b>Attention:</b> ' + html.escape(", ".join(f'{r["name"]} is {r["status"].upper()}' for r in bad)) + ".</div>")
    tiles = "".join(f'<div class="tile"><div class="v">{html.escape(v)}</div><div class="l">{html.escape(label)}</div><div class="s">{sub}</div></div>'
                    for v, label, sub in hero_stats(today, raw_dir, proc_dir, health))
    stubble_daily = _read("stubble_transport_daily.csv", proc_dir)
    feed = ex.latest_feed(raw_dir)
    filled = _read("pm25_hourly_filled.csv", proc_dir)
    health_df = _read("neighbor_station_health.csv", proc_dir)

    fires = ex.fire_daily(raw_dir)
    if fires.empty:
        fire_chart = '<p class="muted">Fire data not available.</p>'
    else:
        fire_chart = ('<div class="legend chart-legend"><span><i class="sw line-sw"></i>Total fire radiative power, all regional detections (left axis, MW)</span>'
                      '<span><i class="sw bar-sw"></i>Detections per day (right axis)</span></div>'
                      + line_bar_svg(list(fires["acq_date"]), [float(v) for v in fires["frp"]], [float(v) for v in fires["fires"]], "Fire radiative power", "Detections"))

    diurnal = ex.diurnal_values(filled)
    diurnal_chart = ("" if diurnal is None else line_bar_svg([f"-----{h:02d}:00" for h in range(24)], diurnal, None, "PM2.5 by hour", "", tick_fmt=lambda t: t[5:7]))

    sat_scatter = '<p class="muted">Satellite or PM data not available.</p>'
    aod = ex.aod_daily(raw_dir)
    if stubble_daily is not None and not aod.empty and "pm25_delhi" in stubble_daily:
        j2 = aod[aod["n"] >= 10].merge(stubble_daily[["date", "pm25_delhi"]].dropna(), on="date")
        sat_scatter = ex.scatter_svg(list(j2["aod"]), list(j2["pm25_delhi"]), list(j2["date"]), "Network-mean satellite AOD (550 nm)", "Delhi PM2.5 (ug/m3)")

    history_india, history_seasons = history_html(proc_dir)
    sec_flow = f"""<section class="card" id="flow"><h2>Is the data flowing?</h2>
<div class="scroll">{health_html(health, today)}</div>
<div class="note"><b>OK</b> = as fresh as that source normally is. <b>GAPS</b> = fresh, but days are missing behind it. <b>STALE / DOWN</b> = nothing new
for longer than usual. A gap in the CPCB feed cannot be recovered from that feed (it serves only the current snapshot); OpenAQ may cover some of it.</div>
</section>"""
    sec_now = f"""<section class="card" id="now"><h2>The network right now</h2>
<div class="sub-h">By city</div>{ex.city_cards_html(feed)}
<div class="split" style="margin-top:22px"><div><div class="sub-h">Station map</div>{ex.station_map_svg(feed, health_df)}</div>
<div><div class="sub-h">Highest and lowest stations</div>{ex.ranking_html(feed)}</div></div>
<div class="note">Colours are the CPCB AQI category of each station&rsquo;s PM2.5 <i>sub-index</i> in the newest feed snapshot (not a concentration). Rings mark stations the health check flagged or put on watch.</div>
</section>"""
    sec_fires = f"""<section class="card" id="fires"><h2>Delhi PM2.5, fires and weather</h2>
<div class="sub-h">Delhi PM2.5 and regional fire exposure</div>
<div class="scroll">{stubble_html(stubble_daily)}</div>
<div class="note warn"><b>Read with care.</b> Delhi PM2.5 stepped up the same day regional fire plumes started arriving, but the days before were
<i>rainy</i> (about 9 mm on 26 Sep, 4 mm on 27 Sep) and 30 Sep was the first fully dry day, so rain stopping explains the step without stubble.
The first look at rain-free days only, with about 10 of them, was moderate and not significant. The pre-registered test on rain-free days has not been run
yet (see the counter below). Suggestive, not established, and <b>not</b> a share of Delhi's pollution attributable to stubble burning.</div>
<div class="sub-h">How close is the real test?</div>{ex.dry_day_counter(stubble_daily)}
<div class="sub-h">Regional fire activity</div><div class="scroll">{fire_chart}</div>
<div class="sub-h">Delhi weather (the controls)</div>{ex.weather_html(stubble_daily)}
</section>"""
    sec_heat = f"""<section class="card" id="heat"><h2>Every station, every day</h2>
<div class="scroll">{ex.heatmap_svg(filled)}</div>
<div class="note">Daily mean PM2.5 from the gap-filled series, stations sorted from highest to lowest overall. A uniform band is a station that matches its neighbours; a block of empty cells is an outage; one odd row is worth a look.</div>
</section>"""
    sec_quality = f"""<section class="card" id="quality"><h2>Data completeness and daily rhythm</h2>
<div class="scroll">{ex.completeness_html(filled)}</div>
<div class="sub-h">Typical day: network-mean PM2.5 by hour (IST)</div><div class="scroll">{diurnal_chart}</div>
</section>"""
    sec_health = f"""<section class="card" id="health"><h2>Station health (neighbour consistency)</h2>
<div class="scroll">{stations_html(health_df)}</div>
<div class="note">A flag means "have someone look", not "faulty". A station beside a road or kiln legitimately differs from its neighbours. Flatlines
(a sensor repeating one exact value for hours) are the clearest evidence; drift needs weeks of data to confirm.</div>
</section>"""
    sec_sat = f"""<section class="card" id="sat"><h2>Agreement with the satellite</h2>
<div class="scroll">{decoupling_html(_read("station_temporal_decoupling.csv", proc_dir))}</div>
<div class="sub-h">Satellite AOD against Delhi PM2.5, day by day</div><div class="scroll">{sat_scatter}</div>
<div class="note">The satellite tracks the city's day-to-day PM2.5 but not differences between stations on the same day, so it can only check whether a
station <i>follows the same day-to-day pattern as its peers</i>. Treat every row as a lead: a result counts only if it survives the multiple-testing correction.</div>
</section>"""
    sec_ops = f"""<section class="card" id="ops"><h2>Pipeline and what&rsquo;s next</h2>
<div class="sub-h">Coming up</div>{ex.countdown_html(today)}
<div class="sub-h">Recent scheduled runs</div>{ex.run_log_html(LOGS_DIR)}
<details class="how"><summary>How to read this page</summary>
<p><b>Sub-index vs concentration.</b> The CPCB feed reports AQI sub-indices; 100 for PM2.5 equals 60 &micro;g/m&sup3;. Charts in &micro;g/m&sup3; come from OpenAQ.</p>
<p><b>Flags are leads, not verdicts.</b> Every statistical result here is exploratory with a few weeks of data; the README lists each limit and every retraction.</p>
<p><b>Nothing here is a source attribution.</b> The stubble panels show association with fire exposure, not the share of pollution caused by it.</p></details>
</section>"""
    body = f"""
<header class="hero">
<div class="brand">{LOGO_SVG}<b class="brand-n">AQI NIS</b></div>
<h1>NCR air-quality network integrity</h1>
<p class="sub">Is the monitoring network telling the truth? Data health, station checks, satellite and fire evidence.</p>
<div class="stamp">Generated {today.isoformat()} &middot; static page, no tracking</div>
<div class="tiles">{tiles}</div>
</header>
{banner}

{tabs_html({"flow": sec_flow, "now": sec_now, "fires": sec_fires, "heat": sec_heat, "quality": sec_quality, "health": sec_health, "sat": sec_sat, "ops": sec_ops, "india": history_india, "seasons": history_seasons})}

{footer_html()}
"""
    return (f'<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
            f'<link rel="icon" href="{FAVICON}">'
            f"<title>NCR AQI network integrity</title><style>{CSS}{tabs_css()}</style></head><body><main>{body}</main></body></html>")


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
