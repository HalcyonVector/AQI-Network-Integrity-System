"""National integrity audit: neighbour consistency and drift for every station-year, then a city league table.

Reuses the live pipeline's checks (scripts/analyze_neighbor_consistency.py): each station's hourly PM2.5 is
predicted leave-one-out from its nearest neighbours (up to 6 within 25 km), with a per-station offset, and
the residuals are tested for stuck sensors, sustained departures, unusual noise and slow drift (Kendall trend
with Benjamini-Hochberg correction). The wind-conditioned predictor is NOT used here: it needs ERA5 winds,
and the live pipeline found the plain neighbour median just as good.

A station with no neighbour within 25 km cannot be checked this way and is reported as "none", not "ok".

Outputs (small, tracked), in data/processed/historical/:
  station_year_audit.csv   one row per station and year
  city_league.csv          one row per city and year: stations audited, failing share, flagged, drifting, outage share

    python historical/national_audit.py
    python historical/national_audit.py --years 2024 2025
"""
import argparse
import re
import sys
from pathlib import Path
from unittest.mock import patch

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import analyze_neighbor_consistency as nc  # noqa: E402  (the live checks)

SILVER = ROOT / "data" / "historical" / "silver"
OUT = ROOT / "data" / "processed" / "historical"
MASTER = OUT / "station_master.csv"


def pm_matrix(silver_year: pd.DataFrame, year: int) -> pd.DataFrame:
    """(hours x stations) PM2.5 on the complete hourly grid of the calendar year (IST)."""
    m = silver_year.pivot(index="hour_start_ist", columns="station_id", values="pm25")
    grid = pd.date_range(f"{year}-01-01", f"{year}-12-31 23:00", freq="h")
    return m.reindex(grid)


MAX_SLOPE_POINTS = 1000


def fast_theilslopes(y, x=None, *args, **kwargs):
    """Theil-Sen slope on at most MAX_SLOPE_POINTS evenly spaced points. Theil-Sen is quadratic in the number
    of points, so a full year of hourly residuals (8,700 points) takes ~15 s per station; a systematic
    subsample of the slowly varying trend gives the same detrending line for a fraction of the cost."""
    y = np.asarray(y)
    x = np.arange(len(y)) if x is None else np.asarray(x)
    if len(y) > MAX_SLOPE_POINTS:
        take = np.linspace(0, len(y) - 1, MAX_SLOPE_POINTS).astype(int)
        y, x = y[take], x[take]
    return _theilslopes(y, x, *args, **kwargs)


_theilslopes = nc.stats.theilslopes


def audit_year(pm: pd.DataFrame, coords: pd.DataFrame) -> pd.DataFrame:
    """Health table for one year. `coords` has station, latitude, longitude."""
    keep = pm.columns[pm.notna().sum() >= nc.MIN_HOURS_PER_STATION]
    pm = pm[keep]
    stations = list(pm.columns)
    neighbors = nc.build_neighbors(coords, stations)
    nan = np.full(pm.shape, np.nan)
    pred = nc.predict_all(np.log(pm.to_numpy(float)), neighbors, nan, nan)["knn_median"]
    with patch.object(nc.stats, "theilslopes", fast_theilslopes):
        health = nc.station_health(pm, pred, neighbors)
    health["hours_in_year"] = len(pm)
    return health


def failing_share(df: pd.DataFrame) -> pd.Series:
    """Share of a station's reported hours inside a stuck-sensor run or a sustained departure from its neighbours."""
    bad = df["flatline_hours"].fillna(0) + df["anomaly_hours"].fillna(0)
    return (bad / df["n_hours"].where(df["n_hours"] > 0)).clip(upper=1)


def city_league(station_year: pd.DataFrame, master: pd.DataFrame) -> pd.DataFrame:
    """One row per city and year. Outage share is the share of expected hours with no reading, over the
    stations that reported anything that year; the failing share is over stations that could be audited."""
    # the audit is keyed by the CPCB station ID, which the master carries as cpcb_id
    df = station_year.merge(master[["cpcb_id", "city", "state"]], left_on="station_id", right_on="cpcb_id", how="left")
    rows = []
    for (city, state, year), g in df.groupby(["city", "state", "year"]):
        audited = g[g["level"].isin(["ok", "watch", "flag"])]
        rows.append({
            "city": city, "state": state, "year": year, "stations": len(g), "stations_audited": len(audited),
            "failing_share": float(audited["failing_share"].mean()) if len(audited) else np.nan,
            "flagged": int((g["level"] == "flag").sum()),
            "drifting": int(g["drifting"].sum()),
            "outage_share": float(1 - g["n_hours"].sum() / g["hours_in_year"].sum()) if g["hours_in_year"].sum() else np.nan,
        })
    return pd.DataFrame(rows).sort_values(["year", "failing_share"], ascending=[True, False])


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--years", type=int, nargs="+")
    ap.add_argument("--league-only", action="store_true", help="rebuild city_league.csv from the existing station_year_audit.csv")
    args = ap.parse_args()
    master = pd.read_csv(MASTER)
    if args.league_only:
        city_league(pd.read_csv(OUT / "station_year_audit.csv"), master).to_csv(OUT / "city_league.csv", index=False)
        print("-> city_league.csv")
        return 0
    coords = master.dropna(subset=["cpcb_id", "latitude"]).rename(columns={"cpcb_id": "station"})[["station", "latitude", "longitude"]]
    years = args.years or sorted(int(re.search(r"(\d{4})\.parquet$", f.name).group(1)) for f in SILVER.glob("pm_hourly_*.parquet"))
    out_path = OUT / "station_year_audit.csv"
    parts = []
    for y in years:
        silver = pd.read_parquet(SILVER / f"pm_hourly_{y}.parquet", columns=["station_id", "hour_start_ist", "pm25"]).dropna(subset=["pm25"])
        h = audit_year(pm_matrix(silver, y), coords)
        h = h.rename(columns={"station": "station_id"})
        h["year"] = y
        h["failing_share"] = failing_share(h)
        h["drifting"] = h["reason"].fillna("").str.contains("drifting vs neighbours")
        parts.append(h)
        print(f"{y}: {len(h)} stations audited; flag {int((h['level'] == 'flag').sum())}, watch {int((h['level'] == 'watch').sum())}, "
              f"ok {int((h['level'] == 'ok').sum())}, no neighbours/too few hours {int((h['level'] == 'none').sum())}", flush=True)
    new = pd.concat(parts, ignore_index=True)
    cols = ["station_id", "year", "n_hours", "hours_in_year", "level", "failing_share", "flatline_hours", "anomaly_hours", "trend_log_per_day",
            "trend_q", "drifting", "resid_sigma_log", "mean_neighbor_km", "reason"]
    new = new[[c for c in cols if c in new.columns]]
    if out_path.exists():  # keep years from earlier runs
        old = pd.read_csv(out_path)
        new = pd.concat([old[~old["year"].isin(new["year"])], new], ignore_index=True)
    new = new.sort_values(["year", "station_id"])
    OUT.mkdir(parents=True, exist_ok=True)
    new.to_csv(out_path, index=False)
    city_league(new, master).to_csv(OUT / "city_league.csv", index=False)
    print(f"-> {out_path} and city_league.csv")
    return 0


if __name__ == "__main__":
    sys.exit(main())
