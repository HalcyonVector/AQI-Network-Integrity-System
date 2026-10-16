"""
Extra dashboard panels (map, heatmap, rankings, fire activity, weather, scatter, run log, ...).

Every function takes plain data (DataFrames / lists) and returns an HTML or inline-SVG string, so each one
is unit-testable and degrades to a muted "not available" note when its data is missing. No JavaScript, no
external requests. All data-derived text is HTML-escaped.
"""

from __future__ import annotations

import glob
import html
import math
import re
from datetime import date, datetime
from pathlib import Path

import pandas as pd

NA = '<p class="muted">{}</p>'

# CPCB AQI categories (upper bound of sub-index, colour, name); colours chosen to read on a dark page
AQI_BANDS = [
    (50, "#22c55e", "Good"),
    (100, "#a3e635", "Satisfactory"),
    (200, "#facc15", "Moderate"),
    (300, "#fb923c", "Poor"),
    (400, "#f43f5e", "Very poor"),
    (10_000, "#c026d3", "Severe"),
]
PM25_LIMIT_SUBINDEX = 100  # sub-index 100 == 60 ug/m3, India's 24 h PM2.5 limit
DRY_DAYS_TARGET = 20  # the circular-shift test needs ~this many rain-free days (see ROADMAP)

# (date, what) for the countdown tiles; the CDS date is a hard external deadline
MILESTONES = [
    (date(2026, 10, 26), "Decide on the Copernicus CDS terms (change takes effect 28 Oct)"),
    (date(2026, 12, 1), "December work starts: stubble test, station health, wrap-up"),
]


def band(v: float) -> tuple[str, str]:
    for hi, colour, name in AQI_BANDS:
        if v <= hi:
            return colour, name
    return AQI_BANDS[-1][1], AQI_BANDS[-1][2]


def _esc(x) -> str:
    return html.escape(str(x))


def _short(name: str, n: int = 26) -> str:
    return name if len(name) <= n else name[: n - 1] + "…"


# ------------------------------------------------------------------ data loading


def latest_feed(raw_dir: Path) -> pd.DataFrame | None:
    files = sorted(glob.glob(str(raw_dir / "cpcb_ncr_*.csv")))
    if not files:
        return None
    try:
        return pd.read_csv(files[-1]).drop_duplicates("station", keep="last")
    except Exception:
        return None


def fire_daily(raw_dir: Path) -> pd.DataFrame:
    """Per acquisition date: detections (low confidence dropped) and total FRP across the whole region."""
    rows = []
    for f in sorted(glob.glob(str(raw_dir / "firms_acq_*.csv"))):
        try:
            d = pd.read_csv(f, usecols=["acq_date", "confidence", "frp"])
        except Exception:
            continue
        d = d[d["confidence"].astype(str).str.lower() != "l"]
        if d.empty:
            continue
        rows.append(d.groupby("acq_date").agg(fires=("frp", "size"), frp=("frp", "sum")).reset_index())
    if not rows:
        return pd.DataFrame(columns=["acq_date", "fires", "frp"])
    return pd.concat(rows).groupby("acq_date", as_index=False).sum().sort_values("acq_date")


def aod_daily(raw_dir: Path) -> pd.DataFrame:
    """Network-mean AOD (550 nm) per date, with how many stations had a retrieval."""
    rows = []
    for f in sorted(glob.glob(str(raw_dir / "aod_ncr_*.csv"))):
        try:
            d = pd.read_csv(f, usecols=["date", "aod_055"])
        except Exception:
            continue
        v = d["aod_055"].dropna()
        if len(v):
            rows.append({"date": str(d["date"].iloc[0]), "aod": float(v.mean()), "n": int(len(v))})
    return pd.DataFrame(rows, columns=["date", "aod", "n"])


# ------------------------------------------------------------------ small charts


def sparkline_card(title: str, dates: list[str], values: list[float | None], unit: str, colour: str,
                   width: int = 300, height: int = 96) -> str:
    """A compact titled line chart with its latest value, min and max."""
    pts = [(i, v) for i, v in enumerate(values) if v is not None and not (isinstance(v, float) and math.isnan(v))]
    if len(pts) < 2:
        return f'<div class="mini"><div class="mini-t">{_esc(title)}</div>{NA.format("Not enough data.")}</div>'
    lo, hi = min(v for _, v in pts), max(v for _, v in pts)
    span = (hi - lo) or 1.0
    n = len(values)
    px = lambda i: 6 + (width - 12) * i / max(n - 1, 1)  # noqa: E731
    py = lambda v: height - 8 - (height - 18) * (v - lo) / span  # noqa: E731
    line = " ".join(f"{px(i):.1f},{py(v):.1f}" for i, v in pts)
    area = f"M{px(pts[0][0]):.1f},{height - 6} L{line.replace(' ', ' L')} L{px(pts[-1][0]):.1f},{height - 6} Z"
    gid = "sp" + re.sub(r"\W", "", title)
    last = pts[-1][1]
    return (f'<div class="mini"><div class="mini-h"><span class="mini-t">{_esc(title)}</span>'
            f'<span class="mini-v">{last:.1f} <small>{_esc(unit)}</small></span></div>'
            f'<svg viewBox="0 0 {width} {height}" class="spark" role="img" aria-label="{_esc(title)}">'
            f'<defs><linearGradient id="{gid}" x1="0" x2="0" y1="0" y2="1"><stop offset="0" stop-color="{colour}" stop-opacity=".4"/>'
            f'<stop offset="1" stop-color="{colour}" stop-opacity="0"/></linearGradient></defs>'
            f'<path d="{area}" fill="url(#{gid})"/><polyline points="{line}" fill="none" stroke="{colour}" stroke-width="2.4" '
            f'stroke-linejoin="round" stroke-linecap="round"/>'
            f'<circle cx="{px(pts[-1][0]):.1f}" cy="{py(last):.1f}" r="3.6" fill="#fff" stroke="{colour}" stroke-width="2"/></svg>'
            f'<div class="mini-r muted"><span>{_esc(dates[pts[0][0]][5:])}</span><span>min {lo:.1f} &middot; max {hi:.1f}</span>'
            f'<span>{_esc(dates[pts[-1][0]][5:])}</span></div></div>')


def weather_html(daily: pd.DataFrame | None) -> str:
    if daily is None or daily.empty:
        return NA.format("Weather data not available.")
    d = daily.sort_values("date")
    dates = [str(x) for x in d["date"]]
    col = lambda c, f=lambda v: v: [None if pd.isna(v) else float(f(v)) for v in d[c]] if c in d else []  # noqa: E731
    cards = [
        sparkline_card("Boundary-layer height (daily max)", dates, col("blh_delhi_max"), "m", "#38bdf8"),
        sparkline_card("Temperature (2 m)", dates, col("t2m_delhi", lambda k: k - 273.15), "°C", "#fb923c"),
        sparkline_card("Wind speed (10 m)", dates, col("wind_delhi"), "m/s", "#a78bfa"),
        sparkline_card("Rain (area mean)", dates, col("precip_delhi_mm"), "mm", "#22d3ee"),
    ]
    return '<div class="minis">' + "".join(cards) + "</div>"


def dry_day_counter(daily: pd.DataFrame | None, exposure_col: str = "hit_frp_925") -> str:
    """Progress toward the number of rain-free days the pre-registered stubble test needs."""
    if daily is None or daily.empty or "washout" not in daily or exposure_col not in daily:
        return NA.format("Rain-free day count not available.")
    d = daily.dropna(subset=["pm25_delhi", exposure_col, "washout"])
    if "in_analysis_window" in d:  # same window the analysis uses (parcels need 72 h of wind first)
        d = d[d["in_analysis_window"].map(lambda v: v is True or str(v) == "True")]
    dry = int((~d["washout"].map(lambda v: v is True or str(v) == "True")).sum())
    pct = min(100.0, 100.0 * dry / DRY_DAYS_TARGET)
    left = max(0, DRY_DAYS_TARGET - dry)
    msg = ("Enough dry days: the pre-registered test can be run once." if left == 0
           else f"{left} more rain-free day{'s' if left != 1 else ''} needed. Do not test before then.")
    return (f'<div class="meter"><div class="meter-top"><b>{dry} / {DRY_DAYS_TARGET}</b> rain-free days with data for the stubble test</div>'
            f'<div class="meter-track"><span class="meter-fill" style="width:{pct:.0f}%"></span></div>'
            f'<div class="muted">{msg}</div></div>')


def countdown_html(today: date) -> str:
    out = ['<div class="count">']
    for d, what in MILESTONES:
        days = (d - today).days
        big = "today" if days == 0 else (f"{days} d" if days > 0 else "passed")
        out.append(f'<div class="count-i"><div class="count-n">{big}</div><div><b>{d:%d %b %Y}</b>'
                   f'<div class="muted">{_esc(what)}</div></div></div>')
    out.append("</div>")
    return "".join(out)


# ------------------------------------------------------------------ station map and rankings


def station_map_svg(feed: pd.DataFrame | None, health: pd.DataFrame | None, width: int = 620) -> str:
    """Stations by lat/lon, filled by PM2.5 sub-index category, ringed red/amber if flagged/watch."""
    if feed is None or feed.empty or not {"latitude", "longitude", "PM2.5", "station"} <= set(feed.columns):
        return NA.format("Station locations not available.")
    f = feed.dropna(subset=["latitude", "longitude"]).copy()
    if f.empty:
        return NA.format("Station locations not available.")
    lat0, lat1 = f["latitude"].min() - 0.03, f["latitude"].max() + 0.03
    lon0, lon1 = f["longitude"].min() - 0.03, f["longitude"].max() + 0.03
    k = math.cos(math.radians((lat0 + lat1) / 2))
    w_deg, h_deg = (lon1 - lon0) * k, lat1 - lat0
    pad = 16
    pw = width - 2 * pad
    ph = pw * h_deg / w_deg
    height = ph + 2 * pad
    X = lambda lon: pad + (lon - lon0) / (lon1 - lon0) * pw  # noqa: E731
    Y = lambda lat: pad + (lat1 - lat) / (lat1 - lat0) * ph  # noqa: E731
    level = {}
    if health is not None and "level" in health:
        level = dict(zip(health["station"], health["level"]))
    top_names = set(f.nlargest(4, "PM2.5")["station"])
    parts = [f'<svg viewBox="0 0 {width} {height:.0f}" class="map" role="img" aria-label="Station map">',
             f'<rect x="{pad}" y="{pad}" width="{pw}" height="{ph:.1f}" rx="14" class="map-bg"/>']
    for i in range(1, 4):  # faint graticule
        parts.append(f'<line x1="{pad + pw * i / 4:.1f}" x2="{pad + pw * i / 4:.1f}" y1="{pad}" y2="{pad + ph:.1f}" class="grid"/>')
        parts.append(f'<line x1="{pad}" x2="{pad + pw}" y1="{pad + ph * i / 4:.1f}" y2="{pad + ph * i / 4:.1f}" class="grid"/>')
    for _, r in f.iterrows():
        pm = r["PM2.5"]
        colour = band(float(pm))[0] if pd.notna(pm) else "#64748b"
        lv = level.get(r["station"])
        ring = {"flag": "#f43f5e", "watch": "#fbbf24"}.get(lv)
        cx, cy = X(r["longitude"]), Y(r["latitude"])
        tip = f'{r["station"]}: PM2.5 sub-index ' + (f"{pm:.0f}" if pd.notna(pm) else "n/a") + (f" ({lv})" if lv in ("flag", "watch") else "")
        if ring:
            parts.append(f'<circle cx="{cx:.1f}" cy="{cy:.1f}" r="12" fill="none" stroke="{ring}" stroke-width="2.5"/>')
        parts.append(f'<circle cx="{cx:.1f}" cy="{cy:.1f}" r="7" fill="{colour}" fill-opacity=".92" stroke="rgba(255,255,255,.7)" stroke-width="1"><title>{_esc(tip)}</title></circle>')
        if ring or r["station"] in top_names:
            left = cx > pad + pw * 0.68  # keep labels inside the frame
            parts.append(f'<text x="{cx + (-15 if left else 15):.1f}" y="{cy + 4:.1f}" class="tick map-lbl" text-anchor="{"end" if left else "start"}">{_esc(_short(str(r["station"]).split(",")[0], 22))}</text>')
    parts.append(f'<text x="{pad + 10}" y="{height - pad - 8:.0f}" class="tick">N &uarr; &middot; {lat0 + 0.03:.2f}-{lat1 - 0.03:.2f}&deg;N, {lon0 + 0.03:.2f}-{lon1 - 0.03:.2f}&deg;E</text>')
    parts.append("</svg>")
    leg = "".join(f'<span><i class="dotc" style="background:{c}"></i>{n}</span>' for _, c, n in AQI_BANDS)
    leg += '<span><i class="dotc ring flagc"></i>flagged</span><span><i class="dotc ring watchc"></i>on watch</span>'
    return "".join(parts) + f'<div class="legend">{leg}</div>'


def city_cards_html(feed: pd.DataFrame | None) -> str:
    if feed is None or feed.empty or "city" not in feed or "PM2.5" not in feed:
        return NA.format("City breakdown not available.")
    g = feed.groupby("city").agg(pm=("PM2.5", "mean"), n=("station", "nunique"), pm10=("PM10", "mean")).dropna(subset=["pm"])
    if g.empty:
        return NA.format("City breakdown not available.")
    out = ['<div class="cities">']
    for city, r in g.sort_values("pm", ascending=False).iterrows():
        colour, name = band(r["pm"])
        out.append(f'<div class="city" style="--c:{colour}"><div class="city-n">{_esc(city)}</div>'
                   f'<div class="city-v">{r["pm"]:.0f}</div><div class="muted">PM2.5 sub-index &middot; {_esc(name)}</div>'
                   f'<div class="muted">{int(r["n"])} stations</div></div>')
    out.append("</div>")
    return "".join(out)


def ranking_html(feed: pd.DataFrame | None, top: int = 12) -> str:
    """Highest and lowest stations right now (bars of the PM2.5 sub-index; 100 marks the 24 h limit)."""
    if feed is None or feed.empty or "PM2.5" not in feed:
        return NA.format("Ranking not available.")
    f = feed.dropna(subset=["PM2.5"]).sort_values("PM2.5", ascending=False)
    if f.empty:
        return NA.format("Ranking not available.")
    rows = pd.concat([f.head(top), f.tail(min(5, max(len(f) - top, 0)))]) if len(f) > top else f
    mx = max(float(rows["PM2.5"].max()), PM25_LIMIT_SUBINDEX * 1.2)
    out = ['<div class="rank">']
    lim = PM25_LIMIT_SUBINDEX / mx * 100
    for i, (_, r) in enumerate(rows.iterrows()):
        if i == top and len(f) > top:
            out.append('<div class="rank-gap muted">&hellip; lowest stations &hellip;</div>')
        colour = band(float(r["PM2.5"]))[0]
        out.append(f'<div class="rank-row"><div class="rank-n" title="{_esc(r["station"])}">{_esc(_short(r["station"]))}</div>'
                   f'<div class="rank-t"><span class="rank-b" style="width:{r["PM2.5"] / mx * 100:.1f}%;background:{colour}"></span>'
                   f'<span class="rank-lim" style="left:{lim:.1f}%"></span></div><div class="rank-v">{r["PM2.5"]:.0f}</div></div>')
    out.append('</div><div class="muted">Bars are the PM2.5 AQI sub-index from the newest feed snapshot. The white tick (100) is India&rsquo;s 24 h limit, 60 &micro;g/m&sup3;.</div>')
    return "".join(out)


# ------------------------------------------------------------------ heatmap and completeness


def heatmap_svg(filled: pd.DataFrame | None, max_stations: int = 70) -> str:
    """Stations (rows) by day (columns), coloured by the daily mean of the gap-filled PM2.5. Days with no
    usable hours stay empty. Rows are sorted by their mean so persistent hot spots group together."""
    if filled is None or filled.empty or not {"hour_start_ist", "station", "pm25_final"} <= set(filled.columns):
        return NA.format("Filled series not available.")
    f = filled.dropna(subset=["pm25_final"]).copy()
    if f.empty:
        return NA.format("Filled series not available.")
    f["day"] = f["hour_start_ist"].astype(str).str[:10]
    piv = f.groupby(["station", "day"])["pm25_final"].mean().unstack("day")
    piv = piv.loc[piv.mean(axis=1).sort_values(ascending=False).index][:max_stations]
    days = list(piv.columns)
    ml, mt, cw, ch, gap = 250, 30, 32, 17, 3
    width = ml + cw * len(days) + 12
    height = mt + ch * len(piv) + 12
    cap = 200.0
    stops = [(0, (34, 197, 94)), (60, (250, 204, 21)), (120, (249, 115, 22)), (160, (244, 63, 94)), (cap, (192, 38, 211))]

    def colour(v: float) -> str:
        v = min(v, cap)
        for (a, ca), (b, cb) in zip(stops, stops[1:]):
            if v <= b:
                t = (v - a) / (b - a)
                return "rgb(%d,%d,%d)" % tuple(round(x + (y - x) * t) for x, y in zip(ca, cb))
        return "rgb(192,38,211)"

    parts = [f'<svg viewBox="0 0 {width} {height}" class="heat" role="img" aria-label="PM2.5 by station and day">']
    for j, d in enumerate(days):
        if j % 2 == 0:
            parts.append(f'<text x="{ml + cw * j + cw / 2:.0f}" y="{mt - 9}" class="tick" text-anchor="middle">{_esc(d[5:])}</text>')
    for i, (name, row) in enumerate(piv.iterrows()):
        y = mt + ch * i
        parts.append(f'<text x="{ml - 8}" y="{y + ch - 4}" class="tick" text-anchor="end">{_esc(_short(name, 34))}</text>')
        for j, d in enumerate(days):
            v = row[d]
            if pd.isna(v):
                parts.append(f'<rect x="{ml + cw * j + 1}" y="{y + 1}" width="{cw - gap}" height="{ch - gap}" rx="3" class="hole"><title>{_esc(name)} {_esc(d)}: no data</title></rect>')
            else:
                parts.append(f'<rect x="{ml + cw * j + 1}" y="{y + 1}" width="{cw - gap}" height="{ch - gap}" rx="3" fill="{colour(float(v))}"><title>{_esc(name)} {_esc(d)}: {v:.0f} &micro;g/m3</title></rect>')
    parts.append("</svg>")
    ramp = ", ".join(f"{colour(v)} {v / cap * 100:.0f}%" for v in (0, 30, 60, 90, 120, 160, 200))
    return ("".join(parts) + f'<div class="ramp"><span>0</span><i style="background:linear-gradient(90deg,{ramp})"></i><span>200+ &micro;g/m&sup3;</span>'
            '<span class="muted">&middot; empty cell = no data that day</span></div>')


def completeness_html(filled: pd.DataFrame | None) -> str:
    """Per day, the share of station-hours that were observed / gap-filled / still missing; plus totals."""
    if filled is None or filled.empty or "source" not in filled:
        return NA.format("Completeness not available.")
    f = filled.copy()
    f["day"] = f["hour_start_ist"].astype(str).str[:10]
    f["kind"] = f["source"].map({"observed": "observed", "neighbour_fill": "filled", "stuck_replaced": "filled"}).fillna("missing")
    t = f.groupby(["day", "kind"]).size().unstack(fill_value=0)
    for c in ("observed", "filled", "missing"):
        if c not in t:
            t[c] = 0
    t = t.div(t.sum(axis=1), axis=0) * 100
    n = len(t)
    width, height, ml, mb, mt = 760, 200, 40, 26, 10
    bw = (width - ml - 8) / n
    ph = height - mb - mt
    colours = {"observed": "#34d399", "filled": "#818cf8", "missing": "#f43f5e"}
    parts = [f'<svg viewBox="0 0 {width} {height}" class="chart" role="img" aria-label="Data completeness by day">']
    for frac in (0, .5, 1):
        y = mt + ph * (1 - frac)
        parts.append(f'<line x1="{ml}" x2="{width - 8}" y1="{y:.1f}" y2="{y:.1f}" class="grid"/><text x="{ml - 6}" y="{y + 4:.1f}" class="tick" text-anchor="end">{frac * 100:.0f}%</text>')
    for i, (day, r) in enumerate(t.iterrows()):
        y = mt + ph
        for kind in ("observed", "filled", "missing"):
            h = ph * r[kind] / 100
            y -= h
            if h > 0:
                parts.append(f'<rect x="{ml + bw * i + 2:.1f}" y="{y:.1f}" width="{bw - 4:.1f}" height="{h:.1f}" fill="{colours[kind]}" fill-opacity=".9"><title>{_esc(day)}: {kind} {r[kind]:.0f}%</title></rect>')
        if i % max(1, n // 8) == 0:
            parts.append(f'<text x="{ml + bw * i + bw / 2:.1f}" y="{height - 8}" class="tick" text-anchor="middle">{_esc(day[5:])}</text>')
    parts.append("</svg>")
    tot = f["kind"].value_counts()
    all_n = int(tot.sum())
    src = f["source"].value_counts()
    stat = lambda k, label, c: f'<div class="stat"><span class="stat-v" style="color:{c}">{tot.get(k, 0) / all_n * 100:.1f}%</span><span class="muted">{label} &middot; {int(tot.get(k, 0)):,} h</span></div>'  # noqa: E731
    extra = (f'<div class="muted">Station-hours by source: ' + ", ".join(f"{_esc(k)} {int(v):,}" for k, v in src.items()) +
             ". Neighbour-filled values carry an expected error of about 24%.</div>")
    legend = ('<div class="legend"><span><i class="sw" style="background:#34d399"></i>observed</span>'
              '<span><i class="sw" style="background:#818cf8"></i>gap-filled</span><span><i class="sw" style="background:#f43f5e"></i>still missing</span></div>')
    return legend + "".join(parts) + '<div class="stats">' + stat("observed", "observed", "#34d399") + stat("filled", "gap-filled", "#818cf8") + stat("missing", "still missing", "#f43f5e") + "</div>" + extra


def diurnal_values(filled: pd.DataFrame | None) -> list[float] | None:
    """Network-mean PM2.5 by hour of day (observed or filled values only)."""
    if filled is None or filled.empty or "pm25_final" not in filled:
        return None
    f = filled.dropna(subset=["pm25_final"])
    if f.empty:
        return None
    hrs = f["hour_start_ist"].astype(str).str[11:13].astype(int)
    return [float(v) for v in f.groupby(hrs)["pm25_final"].mean().reindex(range(24)).interpolate().bfill().ffill()]


# ------------------------------------------------------------------ scatter and fire chart


def _rank(xs: list[float]) -> list[float]:
    return list(pd.Series(xs).rank().values)


def scatter_svg(xs: list[float], ys: list[float], labels: list[str], xlab: str, ylab: str,
                width: int = 520, height: int = 320) -> str:
    n = len(xs)
    if n < 4:
        return NA.format(f"Only {n} paired days so far; a scatter needs at least 4.")
    ml, mb, mt, mr = 54, 44, 14, 16
    pw, ph = width - ml - mr, height - mt - mb
    x0, x1 = min(xs), max(xs)
    y0, y1 = min(ys), max(ys)
    sx = lambda v: ml + (v - x0) / ((x1 - x0) or 1) * pw  # noqa: E731
    sy = lambda v: mt + ph - (v - y0) / ((y1 - y0) or 1) * ph  # noqa: E731
    rho = float(pd.Series(xs).corr(pd.Series(ys), method="spearman"))
    parts = [f'<svg viewBox="0 0 {width} {height}" class="chart" role="img" aria-label="{_esc(xlab)} versus {_esc(ylab)}">']
    for frac in (0, .5, 1):
        y = mt + ph * (1 - frac)
        x = ml + pw * frac
        parts.append(f'<line x1="{ml}" x2="{ml + pw}" y1="{y:.1f}" y2="{y:.1f}" class="grid"/><text x="{ml - 6}" y="{y + 4:.1f}" class="tick" text-anchor="end">{y0 + (y1 - y0) * frac:.0f}</text>')
        parts.append(f'<text x="{x:.1f}" y="{height - 24}" class="tick" text-anchor="middle">{x0 + (x1 - x0) * frac:.2f}</text>')
    parts.append(f'<text x="{ml + pw / 2:.0f}" y="{height - 6}" class="tick" text-anchor="middle">{_esc(xlab)}</text>')
    parts.append(f'<text x="12" y="{mt + ph / 2:.0f}" class="tick" text-anchor="middle" transform="rotate(-90 12 {mt + ph / 2:.0f})">{_esc(ylab)}</text>')
    for x, y, lab in zip(xs, ys, labels):
        parts.append(f'<circle cx="{sx(x):.1f}" cy="{sy(y):.1f}" r="5.5" class="sc"><title>{_esc(lab)}: {x:.2f}, {y:.0f}</title></circle>')
    parts.append("</svg>")
    return "".join(parts) + f'<div class="muted">{n} days &middot; Spearman &rho; = {rho:+.2f}. Few points, so treat the shape, not the number.</div>'


# ------------------------------------------------------------------ pipeline run log


def parse_run_log(text: str) -> list[dict]:
    """Split a run_and_push log into runs: start time, failed steps, committed or not."""
    runs, cur = [], None
    for line in text.splitlines():
        m = re.match(r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2}):\d{2} (.*)", line)
        if not m:
            continue
        ts, msg = m.groups()
        if msg.startswith("Starting CPCB fetch"):
            cur = {"start": ts, "failed": [], "committed": False, "end": ts}
            runs.append(cur)
        elif cur is not None:
            cur["end"] = ts
            fm = re.match(r"(\w+)\.py exited with code", msg)
            if fm:
                cur["failed"].append(fm.group(1))
            if msg.startswith("Committed and pushed"):
                cur["committed"] = True
            if msg.startswith("No changes"):
                cur["committed"] = True
    return runs


def run_log_html(logs_dir: Path, limit: int = 8) -> str:
    runs = []
    for f in sorted(glob.glob(str(logs_dir / "run_and_push_*.log"))):
        try:
            runs += parse_run_log(Path(f).read_text(encoding="utf-8", errors="replace"))
        except OSError:
            continue
    if not runs:
        return NA.format("No run logs found.")
    out = ['<div class="runs">']
    for r in runs[-limit:][::-1]:
        bad = bool(r["failed"])
        done = r["committed"]
        label, cls = ("FAILED STEPS", "down") if bad else (("OK", "ok") if done else ("INCOMPLETE", "stale"))
        detail = ", ".join(sorted(set(r["failed"]))) if bad else ("committed and pushed" if done else "no commit recorded (interrupted?)")
        mins = (datetime.strptime(r["end"], "%Y-%m-%d %H:%M") - datetime.strptime(r["start"], "%Y-%m-%d %H:%M")).total_seconds() / 60
        out.append(f'<div class="run"><span class="chip {cls}">{label}</span><b>{_esc(r["start"])}</b>'
                   f'<span class="muted">{mins:.0f} min &middot; {_esc(detail)}</span></div>')
    out.append("</div>")
    return "".join(out)
