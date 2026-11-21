"""India live tab: the national snapshot feed and the national hourly PM record (OpenAQ), as inline SVG and HTML.

Every function takes loaded DataFrames and returns a string; the page stays one static file with no JavaScript.
Text that comes from data files is HTML-escaped. Missing inputs give a short note, or "" when nothing exists yet.
"""
from __future__ import annotations

import html
import math
from pathlib import Path

import numpy as np
import pandas as pd

import aqi_units
import dashboard_historical as dh

NA = '<p class="muted">{}</p>'
# CPCB PM2.5 AQI categories on concentration (ug/m3): upper bound, colour, label
BANDS = [(30, "#6fbfb2", "0 to 30"), (60, "#a3d2b8", "31 to 60"), (90, "#dfb277", "61 to 90"),
         (120, "#e09a6b", "91 to 120"), (250, "#d9897c", "121 to 250"), (1e9, "#b0566f", "above 250")]
LAT0, LAT1, LON0, LON1 = 6.0, 37.5, 68.0, 98.0


def _esc(x) -> str:
    return html.escape(str(x))


def band(conc: float) -> tuple[str, str]:
    for hi, colour, label in BANDS:
        if conc <= hi:
            return colour, label
    return BANDS[-1][1], BANDS[-1][2]


def read_snapshot(raw_dir: Path) -> pd.DataFrame | None:
    """The newest pull in the newest data/raw/cpcb_india_*.csv, with PM2.5 converted from sub-index to ug/m3."""
    files = sorted(raw_dir.glob("cpcb_india_*.csv"))
    if not files:
        return None
    d = pd.read_csv(files[-1])
    if d.empty:
        return None
    d = d[d["fetched_at_utc"] == d["fetched_at_utc"].max()].copy()
    d["pm25_ugm3"] = aqi_units.concentration_from_subindex("PM2.5", d["PM2.5"].astype(float))
    d["censored"] = aqi_units.is_censored(d["PM2.5"].astype(float))
    return d.dropna(subset=["pm25_ugm3", "latitude", "longitude"])


def _frame(width: int) -> tuple[list[str], float, float]:
    k = math.cos(math.radians(22))
    pw = width - 20
    ph = pw * (LAT1 - LAT0) / ((LON1 - LON0) * k)
    parts = [f'<svg viewBox="0 0 {width} {ph + 20:.0f}" class="map" role="img" aria-label="Map of India">',
             f'<rect x="10" y="10" width="{pw}" height="{ph:.0f}" rx="14" class="map-bg"/>']
    for i in range(1, 6):
        parts.append(f'<line x1="{10 + pw * i / 6:.0f}" x2="{10 + pw * i / 6:.0f}" y1="10" y2="{10 + ph:.0f}" class="grid"/>')
        parts.append(f'<line x1="10" x2="{10 + pw}" y1="{10 + ph * i / 6:.0f}" y2="{10 + ph * i / 6:.0f}" class="grid"/>')
    return parts, pw, ph


def _xy(lat: float, lon: float, pw: float, ph: float) -> tuple[float, float]:
    return 10 + (lon - LON0) / (LON1 - LON0) * pw, 10 + (LAT1 - lat) / (LAT1 - LAT0) * ph


def snapshot_map_svg(snap: pd.DataFrame | None, width: int = 560) -> str:
    if snap is None or snap.empty:
        return NA.format("National snapshot not available.")
    parts, pw, ph = _frame(width)
    for r in snap.sort_values("pm25_ugm3").itertuples():  # worst on top
        if not (LAT0 <= r.latitude <= LAT1 and LON0 <= r.longitude <= LON1):
            continue
        x, y = _xy(r.latitude, r.longitude, pw, ph)
        colour, _ = band(r.pm25_ugm3)
        lbl = f"{r.pm25_ugm3:.0f}{'+' if r.censored else ''} ug/m3"
        parts.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="3.4" fill="{colour}" fill-opacity=".88" stroke="#0b1519" stroke-width=".5"><title>{_esc(r.station)}: {lbl}</title></circle>')
    parts.append("</svg>")
    leg = "".join(f'<span><i class="dotc" style="background:{c}"></i>{l}</span>' for _, c, l in BANDS)
    return "".join(parts) + f'<div class="legend">{leg}</div><p class="muted">PM2.5 in ug/m3, {len(snap)} stations, one dot each.</p>'


def health_map_svg(status: pd.DataFrame | None, width: int = 560) -> str:
    if status is None or status.empty:
        return NA.format("Station health not available.")
    s = status.dropna(subset=["latitude", "longitude"])
    parts, pw, ph = _frame(width)
    for level in ("none", "ok", "watch", "flag"):
        for r in s[s["level"] == level].itertuples():
            if not (LAT0 <= r.latitude <= LAT1 and LON0 <= r.longitude <= LON1):
                continue
            x, y = _xy(r.latitude, r.longitude, pw, ph)
            parts.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="3.4" fill="{dh.LEVEL_COLOUR[level]}" fill-opacity=".88" stroke="#0b1519" stroke-width=".5">'
                         f"<title>{_esc(r.name)}: {dh.LEVEL_LABEL[level]}</title></circle>")
    parts.append("</svg>")
    leg = "".join(f'<span><i class="dotc" style="background:{dh.LEVEL_COLOUR[l]}"></i>{dh.LEVEL_LABEL[l]}</span>' for l in ("ok", "watch", "flag", "none"))
    return "".join(parts) + f'<div class="legend">{leg}</div>'


def daily_line_svg(daily: pd.DataFrame | None, width: int = 640, height: int = 190) -> str:
    """National median station-day PM2.5 by day; the station count is in each point's hover text."""
    if daily is None or len(daily) < 2:
        return NA.format("Not enough days yet for a national trend.")
    d = daily.copy()
    d["date"] = pd.to_datetime(d["date"])
    full = pd.date_range(d["date"].min(), d["date"].max())
    ml, mr, mt, mb = 44, 12, 12, 28
    w, h = width - ml - mr, height - mt - mb
    top = float(max(d["p90_pm25"].max(), 10)) * 1.08
    x = lambda t: ml + (t - full[0]).days / max((full[-1] - full[0]).days, 1) * w  # noqa: E731
    y = lambda v: mt + h - v / top * h  # noqa: E731
    parts = [f'<svg viewBox="0 0 {width} {height}" class="chart" role="img" aria-label="National daily PM2.5">']
    for i in range(5):
        v = top * i / 4
        parts.append(f'<line x1="{ml}" x2="{ml + w}" y1="{y(v):.1f}" y2="{y(v):.1f}" class="grid"/><text x="{ml - 6}" y="{y(v) + 3:.1f}" class="axis" text-anchor="end">{v:.0f}</text>')
    seg, segs = [], []
    for t in full:  # a missing day breaks the line instead of being bridged
        row = d[d["date"] == t]
        if row.empty:
            if len(seg) > 1:
                segs.append(seg)
            seg = []
        else:
            seg.append((x(t), y(float(row["median_pm25"].iloc[0]))))
    if len(seg) > 1:
        segs.append(seg)
    for s in segs:
        parts.append('<polyline fill="none" stroke="#6fbfb2" stroke-width="2" points="' + " ".join(f"{a:.1f},{b:.1f}" for a, b in s) + '"/>')
    for r in d.itertuples():
        parts.append(f'<circle cx="{x(r.date):.1f}" cy="{y(r.median_pm25):.1f}" r="2.4" fill="#6fbfb2"><title>{r.date:%d %b}: median {r.median_pm25:.0f} ug/m3 over {int(r.stations)} stations</title></circle>')
    for t in full[:: max(len(full) // 6, 1)]:
        parts.append(f'<text x="{x(t):.1f}" y="{height - 8}" class="axis" text-anchor="middle">{t:%d %b}</text>')
    parts.append("</svg>")
    return "".join(parts)


def city_table_html(cities: pd.DataFrame | None, top: int = 12) -> str:
    if cities is None or cities.empty:
        return NA.format("City table not available.")
    rows = "".join(f'<tr><td>{_esc(r.city)}<span class="muted"> {_esc(r.state)}</span></td><td class="n">{int(r.stations)}</td>'
                   f'<td class="n">{r.pm25:.0f}</td><td class="n">{r.worst:.0f}</td></tr>' for r in cities.head(top).itertuples())
    return ('<div class="scroll"><table class="tbl"><thead><tr><th>City</th><th class="n">Stations</th><th class="n">Mean PM2.5</th><th class="n">Worst station</th></tr></thead>'
            f"<tbody>{rows}</tbody></table></div>")


def level_counts_html(status: pd.DataFrame | None) -> str:
    if status is None or status.empty:
        return ""
    n = status["level"].value_counts()
    checkable = int(status["level"].isin(["ok", "watch", "flag"]).sum())
    cards = [(f"{len(status)}", "stations in the record"), (f"{checkable}", "could be checked against neighbours"),
             (f"{int(n.get('flag', 0))}", "flagged"), (f"{int(n.get('watch', 0))}", "on watch")]
    return '<div class="tiles">' + "".join(f'<div class="tile"><div class="v">{a}</div><div class="l">{b}</div></div>' for a, b in cards) + "</div>"


def national_html(snap, status, daily, cities, qc, audit_days: int = 30) -> str:
    """The whole tab, or "" when neither the snapshot nor the hourly record exists yet."""
    if snap is None and (status is None or status.empty):
        return ""
    latest = ""
    if status is not None and not status.empty and pd.notna(status["latest_day"].iloc[0]):
        latest = pd.Timestamp(status["latest_day"].iloc[0]).strftime("%d %b %Y")
    removed = ""
    if qc is not None and not qc.empty:
        r = qc[qc["parameter"] == "pm25"].iloc[0]
        removed = (f" Cleaning removed {int(r.removed_range):,} out-of-range and {int(r.removed_flatline):,} stuck-sensor PM2.5 hours "
                   f"({(r.removed_range + r.removed_flatline) / max(r.hours_raw, 1) * 100:.1f}% of {int(r.hours_raw):,}).")
    return f"""<section class="card" id="indialive"><h2>India, live</h2>
<p class="muted" style="margin-top:-6px">The same checks as the NCR network, applied to the CPCB stations across the country as they report.</p>
<div class="sub-h">Right now: every station in the national feed</div>
<div class="split"><div>{snapshot_map_svg(snap)}</div><div><div class="sub-h">Where the day&rsquo;s hourly record stands</div>{level_counts_html(status)}
<div class="note">The hourly record comes from OpenAQ, which publishes about a week late, so its latest full day is {latest or "not yet known"}.{removed}</div></div></div>

<div class="sub-h">National level by day</div>{daily_line_svg(daily)}
<div class="note">Median of station-day means (a day needs at least 18 hours). Hover a point for the station count. The line breaks where no day was
available instead of bridging it.</div>

<div class="split" style="margin-top:22px"><div><div class="sub-h">Highest cities on the latest full day</div>{city_table_html(cities)}
<div class="note">Cities with at least two stations reporting that day.</div></div>
<div><div class="sub-h">Station health, last {audit_days} days</div>{health_map_svg(status)}</div></div>
<div class="note warn"><b>Read with care.</b> The map above shows the feed&rsquo;s PM2.5 sub-index converted back to a concentration; it is capped at 500 on the
index scale (about 500 ug/m3), so very high values are lower bounds. A flag means &ldquo;have someone look&rdquo;, not &ldquo;faulty&rdquo;. Stations with no
neighbour within 25 km cannot be checked and are shown in grey.</div>
</section>"""
