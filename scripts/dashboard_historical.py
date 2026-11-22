"""Historical section of the dashboard: 2015-2025 backfill health, national integrity audit and Delhi seasons.

Every function takes already-loaded DataFrames (or None) and returns an HTML or inline-SVG string, so the
page stays one static file with no JavaScript. Missing inputs degrade to a short "not available" note.
Values that come from data files are HTML-escaped.
"""
from __future__ import annotations

import html
import math

import numpy as np
import pandas as pd

NA = '<p class="muted">{}</p>'
LEVEL_COLOUR = {"ok": "#6fbfb2", "watch": "#dfb277", "flag": "#d9897c", "none": "#6b7b7e"}
LEVEL_LABEL = {"ok": "consistent with neighbours", "watch": "on watch", "flag": "flagged", "none": "cannot be checked (no neighbour within 25 km)"}


def _esc(x) -> str:
    return html.escape(str(x))


def backfill_html(qc: pd.DataFrame | None, sources: list[tuple[str, int, int, str]]) -> str:
    """Source cards (done of total files) and a per-year table of what survived cleaning."""
    cards = []
    for name, done, total, note in sources:
        ok = done >= total
        cls = "ok" if ok else ("gaps" if done else "none")
        cards.append(f'<div class="src"><div class="src-top"><span class="src-name">{_esc(name)}</span>'
                     f'<span class="chip {cls}">{"complete" if ok else "waiting" if not done else "partial"}</span></div>'
                     f'<div class="src-meta"><span><b>{done} of {total}</b> files</span></div><div class="muted">{_esc(note)}</div></div>')
    out = '<div class="sources">' + "".join(cards) + "</div>"
    if qc is None or qc.empty:
        return out + NA.format("Cleaning summary not available.")
    rows = "".join(
        f'<tr><td>{int(r.year)}</td><td class="n">{int(r.stations)}</td><td class="n">{int(r.hours_clean):,}</td>'
        f'<td class="n">{r.removed_flatline / r.hours_raw * 100:.1f}%</td></tr>' for r in qc.sort_values("year").itertuples())
    return (out + '<div class="sub-h">What survived cleaning, by year</div><div class="scroll"><table class="tbl"><thead><tr><th>Year</th>'
            '<th class="n">Stations with PM2.5</th><th class="n">Clean station-hours</th><th class="n">Stuck-sensor hours removed</th></tr></thead>'
            f"<tbody>{rows}</tbody></table></div>")


def completeness_matrix_svg(comp: pd.DataFrame | None) -> str:
    """State x month heatmap of the share of hours reported; months with no station stay a neutral hole."""
    if comp is None or comp.empty:
        return NA.format("Completeness data not available.")
    months = sorted(comp["month"].unique())
    first, last = pd.Period(months[0]), pd.Period(months[-1])
    grid = [str(first + i) for i in range((last - first).n + 1)]
    states = comp.groupby("state")["stations"].max().sort_values(ascending=False).index.tolist()
    lookup = {(r.state, r.month): (r.completeness, r.stations) for r in comp.itertuples()}
    ml, mt, cw, ch = 150, 26, 7, 13
    width, height = ml + cw * len(grid) + 30, mt + ch * len(states) + 8  # right margin so the last year label is not clipped
    parts = [f'<svg viewBox="0 0 {width} {height}" class="heat" role="img" aria-label="Share of hours reported, by state and month">']
    for j, m in enumerate(grid):
        if m.endswith("-01"):
            parts.append(f'<text x="{ml + cw * j}" y="{mt - 8}" class="tick">{m[:4]}</text>')
    for i, s in enumerate(states):
        y = mt + ch * i
        parts.append(f'<text x="{ml - 6}" y="{y + ch - 3}" class="tick" text-anchor="end">{_esc(s[:22])}</text>')
        for j, m in enumerate(grid):
            v = lookup.get((s, m))
            x = ml + cw * j
            if v is None:
                parts.append(f'<rect x="{x}" y="{y}" width="{cw - 1}" height="{ch - 1}" rx="1.5" class="hole"/>')
            else:
                a = 0.15 + 0.85 * min(max(v[0], 0), 1)
                parts.append(f'<rect x="{x}" y="{y}" width="{cw - 1}" height="{ch - 1}" rx="1.5" fill="#6fbfb2" fill-opacity="{a:.2f}">'
                             f"<title>{_esc(s)} {m}: {v[0] * 100:.0f}% of hours, {int(v[1])} station(s)</title></rect>")
    parts.append("</svg>")
    return "".join(parts) + '<div class="legend"><span>brighter = more of the month reported</span><span>empty cell = no station reported</span></div>'


def india_map_svg(master: pd.DataFrame | None, audit: pd.DataFrame | None, year: int, width: int = 560) -> str:
    """One dot per audited station for one year, coloured by audit level, larger for longer records."""
    if master is None or audit is None or master.empty or audit.empty:
        return NA.format("Station audit not available.")
    a = audit[audit["year"] == year].merge(master.dropna(subset=["latitude", "longitude"])[["cpcb_id", "latitude", "longitude", "name", "years_with_pm25"]],
                                           left_on="station_id", right_on="cpcb_id")
    if a.empty:
        return NA.format(f"No audited stations with coordinates in {year}.")
    lat0, lat1, lon0, lon1 = 6.0, 37.5, 68.0, 98.0
    k = math.cos(math.radians(22))
    pw = width - 20
    ph = pw * (lat1 - lat0) / ((lon1 - lon0) * k)
    parts = [f'<svg viewBox="0 0 {width} {ph + 20:.0f}" class="map" role="img" aria-label="Station integrity map of India, {year}">',
             f'<rect x="10" y="10" width="{pw}" height="{ph:.0f}" rx="14" class="map-bg"/>']
    for i in range(1, 6):
        parts.append(f'<line x1="{10 + pw * i / 6:.0f}" x2="{10 + pw * i / 6:.0f}" y1="10" y2="{10 + ph:.0f}" class="grid"/>')
        parts.append(f'<line x1="10" x2="{10 + pw}" y1="{10 + ph * i / 6:.0f}" y2="{10 + ph * i / 6:.0f}" class="grid"/>')
    # drawn in order so the notable stations sit on top of the unremarkable ones
    for level in ("none", "ok", "watch", "flag"):
        for r in a[a["level"] == level].itertuples():
            x = 10 + (r.longitude - lon0) / (lon1 - lon0) * pw
            y = 10 + (lat1 - r.latitude) / (lat1 - lat0) * ph
            rad = 2.2 + min(int(r.years_with_pm25), 10) * 0.35
            parts.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="{rad:.1f}" fill="{LEVEL_COLOUR[level]}" fill-opacity=".85" stroke="#0b1519" stroke-width=".6">'
                         f"<title>{_esc(r.name)}: {LEVEL_LABEL[level]}, {int(r.years_with_pm25)} year(s) of PM2.5</title></circle>")
    parts.append("</svg>")
    leg = "".join(f'<span><i class="dotc" style="background:{LEVEL_COLOUR[l]}"></i>{LEVEL_LABEL[l]}</span>' for l in ("ok", "watch", "flag", "none"))
    n = a["level"].value_counts()
    return "".join(parts) + f'<div class="legend">{leg}</div><p class="muted">{year}: {len(a)} stations with coordinates; larger dot = longer PM2.5 record.</p>'


def league_html(league: pd.DataFrame | None, year: int, min_audited: int = 3, top: int = 15) -> str:
    """Cities with the largest share of station-hours flagged by the neighbour checks, worst first."""
    if league is None or league.empty:
        return NA.format("City league not available.")
    d = league[(league["year"] == year) & (league["stations_audited"] >= min_audited)].sort_values("failing_share", ascending=False).head(top)
    if d.empty:
        return NA.format(f"No city had at least {min_audited} auditable stations in {year}.")
    rows = "".join(
        f'<tr><td>{_esc(r.city)}<span class="muted"> {_esc(r.state)}</span></td><td class="n">{int(r.stations_audited)} of {int(r.stations)}</td>'
        f'<td class="n">{r.failing_share * 100:.1f}%</td><td class="n">{int(r.flagged)}</td><td class="n">{int(r.drifting)}</td>'
        f'<td class="n">{r.outage_share * 100:.0f}%</td></tr>' for r in d.itertuples())
    return ('<div class="scroll"><table class="tbl"><thead><tr><th>City</th><th class="n">Stations audited</th><th class="n">Hours flagged</th>'
            '<th class="n">Stations flagged</th><th class="n">Drifting</th><th class="n">Hours missing</th></tr></thead>'
            f"<tbody>{rows}</tbody></table></div>")


def season_multiples_svg(delhi: pd.DataFrame | None, fires: pd.DataFrame | None, years: list[int]) -> str:
    """Small multiples, one per burning season: Delhi daily PM2.5 (line) over regional fire detections (bars), 1 Oct to 30 Nov."""
    if delhi is None or fires is None or delhi.empty or fires.empty:
        return NA.format("Season data not available.")
    d = delhi.copy()
    d["date"] = pd.to_datetime(d["date"])
    f = fires[fires["source"] == "VIIRS_SNPP_SP"].copy()
    f["date"] = pd.to_datetime(f["date"])
    pm_max = float(d[d["date"].dt.month.isin([10, 11])]["pm25"].max())
    fire_max = float(f["detections"].max()) or 1.0
    cols, pw, ph, gap = 3, 300, 96, 34
    rows = math.ceil(len(years) / cols)
    W, H = cols * (pw + 24) + 10, rows * (ph + gap) + 6
    parts = [f'<svg viewBox="0 0 {W} {H}" class="chart" role="img" aria-label="Delhi PM2.5 and regional fire detections for each season">']
    for n, y in enumerate(years):
        ox, oy = 10 + (n % cols) * (pw + 24), 18 + (n // cols) * (ph + gap)
        start = pd.Timestamp(f"{y}-10-01")
        pm = d[(d["date"] >= start) & (d["date"] <= f"{y}-11-30")].sort_values("date")
        fr = f[(f["date"] >= start) & (f["date"] <= f"{y}-11-30")]
        parts.append(f'<text x="{ox}" y="{oy - 6}" class="tick">{y}</text>')
        parts.append(f'<line x1="{ox}" x2="{ox + pw}" y1="{oy + ph}" y2="{oy + ph}" class="grid"/>')
        bw = pw / 61
        for r in fr.itertuples():
            i = (r.date - start).days
            h = r.detections / fire_max * ph
            parts.append(f'<rect x="{ox + i * bw:.1f}" y="{oy + ph - h:.1f}" width="{max(bw - 1, 1):.1f}" height="{h:.1f}" class="bar"><title>{r.date:%d %b}: {int(r.detections)} fire detections</title></rect>')
        pts = " ".join(f"{ox + (r.date - start).days * bw + bw / 2:.1f},{oy + ph - r.pm25 / pm_max * ph:.1f}" for r in pm.itertuples())
        if pts:
            parts.append(f'<polyline points="{pts}" class="line" style="stroke-width:1.8"/>')
    parts.append("</svg>")
    return ("".join(parts) + '<div class="legend chart-legend"><span><i class="sw line-sw"></i>Delhi PM2.5, daily mean (same scale in every panel)</span>'
            '<span><i class="sw bar-sw"></i>Regional fire detections per day, Suomi NPP (same scale in every panel)</span></div>')


def _shift_text(shift: float) -> str:
    """A straight-line drift over a year compounds, so a short or noisy record can extrapolate to absurd percentages; cap the display."""
    if shift > 10:
        return "more than +1,000%"
    return f"{shift * 100:+.0f}%"


def drift_watchlist_html(audit: pd.DataFrame | None, master: pd.DataFrame | None, top: int = 12) -> str:
    """Stations whose residual drifted against their neighbours (confirmed after correction), most years first."""
    if audit is None or master is None or audit.empty:
        return NA.format("Audit not available.")
    d = audit[audit["drifting"].astype(bool)]
    if d.empty:
        return NA.format("No station showed confirmed drift against its neighbours.")
    last_year = int(audit["year"].max())
    if "trend_days" not in d:
        d = d.assign(trend_days=365)
    d = d.assign(shift=lambda x: np.expm1(x["trend_log_per_day"] * (x["trend_days"].fillna(365) - 1)))
    rows_out = []
    for sid, g in d.groupby("station_id"):
        worst = g.loc[g["shift"].abs().idxmax()]  # the year with the largest total shift
        y_last = int(g["year"].max())
        later = audit[(audit["station_id"] == sid) & (audit["year"] > y_last) & audit["level"].isin(["ok", "watch", "flag"])]
        if y_last == last_year:
            status = f"unresolved: still drifting in {last_year}"
        elif len(later):  # y_last is the last drifting year, so every later audited year was clean
            status = "cleared: not drifting since"
        else:
            status = "unresolved: no later data to check"
        rows_out.append((sid, ", ".join(str(int(y)) for y in sorted(g["year"])), len(g), float(worst["shift"]), int(worst["year"]), status))
    out = pd.DataFrame(rows_out, columns=["station_id", "years", "n", "shift", "shift_year", "status"]).sort_values(["n", "shift"], key=lambda c: c.abs() if c.name == "shift" else c, ascending=False).head(top)
    out = out.merge(master[["cpcb_id", "name", "city"]], left_on="station_id", right_on="cpcb_id", how="left")
    rows = "".join(f'<tr><td>{_esc(r.name)}<span class="muted"> {_esc(r.city)}</span></td><td>{_esc(r.years)}</td>'
                   f'<td class="n">{_shift_text(r.shift)} over {r.shift_year}</td><td>{_esc(r.status)}</td></tr>' for r in out.itertuples())
    return ('<div class="scroll"><table class="tbl"><thead><tr><th>Station</th><th>Years drifting</th><th class="n">Largest shift vs neighbours</th><th>Status</th></tr></thead>'
            f"<tbody>{rows}</tbody></table></div>")
