"""
Quality control and station health for the live national PM record (OpenAQ, ~500 CPCB stations).

Reads data/raw/openaq_india_pm_*.parquet and
  1. cleans each station's hourly series with the same range and flatline rules as the NCR pipeline
     (scripts/qc.py): readings outside (0, PM_MAX] and stuck runs of 6+ identical hours are removed;
  2. audits the last AUDIT_DAYS days with the neighbour-consistency checks (leave-one-out prediction from up to
     6 stations within 25 km, offset, anomaly hours, Theil-Sen drift with Benjamini-Hochberg control), the same
     routine as the 2015 to 2025 national audit;
  3. summarises the network: per-station status, a national daily level, a city table for the latest day.

Writes data/processed/national/{qc_summary,station_status,national_daily,city_latest}.csv. The raw files carry no
QC; everything here is derived and can be rebuilt.

    python scripts/analyze_national_live.py
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "historical"))

import qc  # noqa: E402

RAW = ROOT / "data" / "raw"
OUT = ROOT / "data" / "processed" / "national"
MASTER = ROOT / "data" / "processed" / "historical" / "station_master.csv"
AUDIT_DAYS = 30
MIN_HOURS_PER_DAY = 18


def load_matrices(raw_dir: Path = RAW) -> dict[str, pd.DataFrame]:
    """(hours x OpenAQ location) matrices for pm25 and pm10 on one complete hourly IST grid."""
    frames = []
    for f in sorted(raw_dir.glob("openaq_india_pm_*.parquet")):
        d = pd.read_parquet(f)
        if d.empty:
            continue
        day = pd.Timestamp(f.stem.rsplit("_", 1)[1])
        d["ts"] = day + pd.to_timedelta(d["hour"].astype(int), unit="h")
        frames.append(d)
    if not frames:
        return {}
    d = pd.concat(frames, ignore_index=True)
    grid = pd.date_range(d["ts"].min(), d["ts"].max().normalize() + pd.Timedelta(hours=23), freq="h")
    return {p: d.pivot_table(index="ts", columns="openaq_id", values=p, aggfunc="mean").reindex(grid) for p in ("pm25", "pm10")}


def clean(pm: pd.DataFrame) -> tuple[pd.DataFrame, dict]:
    """Range check then flatline check; returns the cleaned matrix and the counts removed."""
    raw_n = int(pm.notna().sum().sum())
    in_range = pm.where((pm > 0) & (pm <= qc.PM_MAX))
    flat = qc.mask_flatlines(in_range)
    n_range = int(in_range.notna().sum().sum())
    return flat, {"hours_raw": raw_n, "removed_range": raw_n - n_range, "removed_flatline": n_range - int(flat.notna().sum().sum()),
                  "hours_clean": int(flat.notna().sum().sum())}


def daily_summary(pm: pd.DataFrame) -> pd.DataFrame:
    """Per date: stations with a valid day (at least MIN_HOURS_PER_DAY hours), their median and mean station-day PM2.5."""
    day = pm.index.normalize()
    counts = pm.groupby(day).count()
    means = pm.groupby(day).mean().where(counts >= MIN_HOURS_PER_DAY)
    out = pd.DataFrame({"stations": means.notna().sum(axis=1), "median_pm25": means.median(axis=1), "mean_pm25": means.mean(axis=1),
                        "p90_pm25": means.quantile(0.9, axis=1)})
    out.index.name = "date"
    return out.dropna(subset=["median_pm25"]).reset_index()


def station_status(pm25: pd.DataFrame, health: pd.DataFrame, master: pd.DataFrame) -> pd.DataFrame:
    """One row per OpenAQ-matched station: last reading, recent completeness, latest day level, audit result."""
    day = pm25.index.normalize()
    counts = pm25.groupby(day).count()
    means = pm25.groupby(day).mean().where(counts >= MIN_HOURS_PER_DAY)
    latest_day = means.dropna(how="all").index.max()
    last_seen = pm25.apply(lambda s: s.last_valid_index())
    window = pm25.iloc[-AUDIT_DAYS * 24:]
    rows = pd.DataFrame({
        "openaq_id": pm25.columns,
        "last_hour": [last_seen[c] for c in pm25.columns],
        "hours_audit_window": window.notna().sum().to_numpy(),
        "completeness": (window.notna().sum() / max(len(window), 1)).to_numpy(),
        "latest_day": latest_day,
        "pm25_latest_day": means.loc[latest_day].to_numpy() if pd.notna(latest_day) else np.nan,
    })
    if not health.empty:
        h = health.rename(columns={"station": "openaq_id"})
        h["failing_share"] = ((h["flatline_hours"].fillna(0) + h["anomaly_hours"].fillna(0)) / h["n_hours"].where(h["n_hours"] > 0)).clip(upper=1)
        h["drifting"] = h["reason"].fillna("").str.contains("drifting vs neighbours")
        rows = rows.merge(h[["openaq_id", "level", "failing_share", "drifting", "reason"]], on="openaq_id", how="left")
    else:
        rows["level"], rows["failing_share"], rows["drifting"], rows["reason"] = "none", np.nan, False, ""
    rows["level"] = rows["level"].fillna("none")
    m = master.dropna(subset=["openaq_id"]).assign(openaq_id=lambda d: d["openaq_id"].astype(int))
    m = m.drop_duplicates("openaq_id")[["openaq_id", "name", "city", "state", "latitude", "longitude"]]
    return rows.merge(m, on="openaq_id", how="left").sort_values(["state", "city", "name"], ignore_index=True)


def city_latest(status: pd.DataFrame, min_stations: int = 2) -> pd.DataFrame:
    d = status.dropna(subset=["pm25_latest_day", "city"])
    g = d.groupby(["city", "state"]).agg(stations=("openaq_id", "count"), pm25=("pm25_latest_day", "mean"), worst=("pm25_latest_day", "max")).reset_index()
    return g[g["stations"] >= min_stations].sort_values("pm25", ascending=False, ignore_index=True)


def main() -> int:
    mats = load_matrices()
    if not mats:
        print("No data/raw/openaq_india_pm_*.parquet yet; run scripts/fetch_openaq_india.py.")
        return 0
    master = pd.read_csv(MASTER)
    cleaned, qc_rows = {}, []
    for p, m in mats.items():
        cleaned[p], st = clean(m)
        qc_rows.append({"parameter": p, **st})
    pm25 = cleaned["pm25"].dropna(how="all", axis=1)

    import national_audit as na  # noqa: E402  (the 2015 to 2025 audit routine)
    window = pm25.iloc[-AUDIT_DAYS * 24:]
    coords = (master.dropna(subset=["openaq_id", "latitude"]).assign(station=lambda d: d["openaq_id"].astype(int))
              .drop_duplicates("station")[["station", "latitude", "longitude"]])
    coords = coords[coords["station"].isin(window.columns)]
    health = na.audit_year(window, coords) if len(coords) >= 2 else pd.DataFrame()

    status = station_status(pm25, health, master)
    OUT.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(qc_rows).to_csv(OUT / "qc_summary.csv", index=False)
    status.to_csv(OUT / "station_status.csv", index=False)
    daily = daily_summary(pm25)
    daily.to_csv(OUT / "national_daily.csv", index=False)
    city_latest(status).to_csv(OUT / "city_latest.csv", index=False)
    lv = status["level"].value_counts().to_dict()
    print(f"{len(pm25.columns)} stations, {len(daily)} days; audit levels {lv}; QC removed "
          f"{qc_rows[0]['removed_range']:,} out of range and {qc_rows[0]['removed_flatline']:,} flatlined PM2.5 hours")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
