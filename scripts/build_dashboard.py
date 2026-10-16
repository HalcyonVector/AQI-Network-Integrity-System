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


def line_bar_svg(dates: list[str], line: list[float | None], bars: list[float | None],
                 line_label: str, bar_label: str, width: int = 760, height: int = 280,
                 rain: list[float | None] | None = None, threshold: float | None = None) -> str:
    """Line (left axis) over bars (right axis), as an SVG string. None values leave gaps.

    Optional: `rain` draws cyan bars hanging from the top (own scale, at most a third of the height);
    `threshold` draws a dashed reference line on the left axis (e.g. the PM2.5 standard)."""
    ml, mr, mt, mb = 52, 52, 30, 44
    pw, ph = width - ml - mr, height - mt - mb
    n = len(dates)
    if n == 0:
        return '<p class="muted">No data to chart.</p>'
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
        parts.append(f'<text x="{x(i):.1f}" y="{base + 18}" class="tick" text-anchor="middle">{html.escape(dates[i][5:])}</text>')
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


CSS = """:root{--bg1:#05060f;--bg2:#0b0f24;--bg3:#07202a;
--card:linear-gradient(145deg,rgba(255,255,255,.085),rgba(255,255,255,.025));--fg:#e6eaf7;--muted:#98a3c2;--line:rgba(255,255,255,.12);
--shadow:0 18px 44px -18px rgba(0,0,0,.75),inset 0 1px 0 rgba(255,255,255,.14),inset 0 0 0 1px rgba(255,255,255,.03);
--tile:rgba(255,255,255,.09);--tile-line:rgba(255,255,255,.2);--cell-pre:rgba(255,255,255,.08);--grid:rgba(255,255,255,.09)}
*{box-sizing:border-box}
html{background:var(--bg1)}
body{margin:0;color:var(--fg);font:15px/1.55 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;min-height:100vh;
background:radial-gradient(900px 600px at 6% 0%,rgba(79,70,229,.38),transparent 62%),radial-gradient(800px 560px at 100% 18%,rgba(13,148,170,.28),transparent 60%),radial-gradient(900px 600px at 20% 100%,rgba(126,34,206,.26),transparent 62%),linear-gradient(160deg,var(--bg1),var(--bg2) 50%,var(--bg3));background-attachment:fixed}
main{max-width:1000px;margin:0 auto;padding:22px 16px 56px}
.hero{position:relative;overflow:hidden;border-radius:26px;padding:34px 30px 26px;color:#fff;
background:radial-gradient(520px 260px at 90% -10%,rgba(255,255,255,.28),transparent 65%),linear-gradient(125deg,rgba(55,48,163,.85) 0%,rgba(91,33,182,.8) 40%,rgba(14,94,116,.85) 100%);
border:1px solid rgba(255,255,255,.18);box-shadow:0 26px 60px -26px rgba(79,70,229,.7),inset 0 1px 0 rgba(255,255,255,.25);backdrop-filter:blur(18px);-webkit-backdrop-filter:blur(18px)}
.hero:after{content:"";position:absolute;inset:auto -60px -90px auto;width:300px;height:300px;border-radius:50%;background:radial-gradient(circle,rgba(34,211,238,.45),transparent 65%)}
.hero h1{margin:0;font-size:clamp(26px,4.6vw,38px);line-height:1.1;letter-spacing:-.025em;font-weight:800;position:relative;z-index:1}
.hero .sub{margin:8px 0 0;max-width:640px;opacity:.9;font-size:15px;position:relative;z-index:1}
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
.foot{margin-top:30px;text-align:center;font-size:12.5px;color:var(--muted)}
.scroll{overflow-x:auto}
@media (max-width:760px){.tiles{grid-template-columns:repeat(2,1fr)}.hero{padding:26px 20px 20px}.card{padding:18px 16px 14px}
.g-row{grid-template-columns:1fr 70px;gap:6px 12px}.g-track{grid-column:1 / -1;order:3}.g-head{display:none}}
"""


def render(today: date, raw_dir: Path = RAW_DIR, proc_dir: Path = PROC_DIR) -> str:
    health = all_source_health(today, raw_dir)
    bad = [r for r in health if r["status"] in ("gaps", "stale", "down", "none")]
    banner = ("" if not bad else '<div class="banner"><b>Attention:</b> ' + html.escape(", ".join(f'{r["name"]} is {r["status"].upper()}' for r in bad)) + ".</div>")
    tiles = "".join(f'<div class="tile"><div class="v">{html.escape(v)}</div><div class="l">{html.escape(label)}</div><div class="s">{sub}</div></div>'
                    for v, label, sub in hero_stats(today, raw_dir, proc_dir, health))
    stubble_daily = _read("stubble_transport_daily.csv", proc_dir)
    body = f"""
<header class="hero">
<h1>NCR air-quality network integrity</h1>
<p class="sub">Is the monitoring network telling the truth? Data health, station checks, and what the satellite and fires say about Delhi's PM2.5.</p>
<div class="stamp">Generated {today.isoformat()} &middot; static page, no tracking</div>
<div class="tiles">{tiles}</div>
</header>
{banner}

<section class="card"><h2><span class="num">1</span>Is the data flowing?</h2>
<div class="scroll">{health_html(health, today)}</div>
<div class="note"><b>OK</b> = as fresh as that source normally is. <b>GAPS</b> = fresh, but days are missing behind it. <b>STALE / DOWN</b> = nothing new
for longer than usual. A gap in the CPCB feed cannot be recovered from that feed (it serves only the current snapshot); OpenAQ may cover some of it.</div>
</section>

<section class="card"><h2><span class="num">2</span>Delhi PM2.5 and regional fire exposure</h2>
<div class="scroll">{stubble_html(stubble_daily)}</div>
<div class="note warn"><b>Read with care.</b> Delhi PM2.5 stepped up the same day regional fire plumes started arriving, but the days before were
<i>rainy</i> (about 9 mm on 26 Sep, 4 mm on 27 Sep) and 30 Sep was the first fully dry day, so rain stopping explains the step without stubble.
On rain-free days the association is moderate and not yet significant (about 10 days). Suggestive, not established, and <b>not</b> a share of Delhi's
pollution attributable to stubble burning.</div>
</section>

<section class="card"><h2><span class="num">3</span>Station health (neighbour consistency)</h2>
<div class="scroll">{stations_html(_read("neighbor_station_health.csv", proc_dir))}</div>
<div class="note">A flag means "have someone look", not "faulty". A station beside a road or kiln legitimately differs from its neighbours. Flatlines
(a sensor repeating one exact value for hours) are the clearest evidence; drift needs weeks of data to confirm.</div>
</section>

<section class="card"><h2><span class="num">4</span>Agreement with the satellite</h2>
<div class="scroll">{decoupling_html(_read("station_temporal_decoupling.csv", proc_dir))}</div>
<div class="note">The satellite tracks the city's day-to-day PM2.5 but not differences between stations on the same day, so it can only check whether a
station <i>follows the same day-to-day pattern as its peers</i>. With about two weeks of data, no station is statistically distinguishable yet.</div>
</section>

<div class="foot">Built from the project's own pipelines &middot; every number has a caveat in the README</div>
"""
    return (f'<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
            f"<title>NCR AQI network integrity</title><style>{CSS}</style></head><body><main>{body}</main></body></html>")


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
