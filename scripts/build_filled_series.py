"""
Cleaned, gap-filled hourly PM2.5 per station, where every value says what it is.

Fills only what the backtest (analyze_neighbor_consistency.py) actually validated:
  * INTERIOR gaps (a real reading before and after) of at most MAX_FILL_HOURS. Leading and
    trailing blanks are a station not yet reporting / offline, not a gap, and stay blank.
  * from the neighbour median (leave-one-out, per-site offset), only when enough neighbours
    report that hour.
Backtested typical error is roughly 22-26% of the level, so each filled value carries an
`expected_abs_error_pct`. Observed values are NEVER modified.

`source` column:
  observed       a real measurement
  neighbour_fill estimated from neighbours (an estimate, not a measurement)
  stuck_replaced a stuck-sensor value was removed and replaced by a neighbour estimate
  stuck_removed  a stuck-sensor value was removed and could not be replaced
  unfilled       missing: too long a gap, no neighbours, or outside the station's record

NOT covered: a network-wide outage (no neighbours reporting). Only a regional daily estimate
from the satellite is possible there, and it is not validated.

Usage:
    python scripts/build_filled_series.py
Writes data/processed/pm25_hourly_filled.csv.
"""

from __future__ import annotations

import glob
import sys
from pathlib import Path

import numpy as np
import pandas as pd

from analyze_neighbor_consistency import (
    build_neighbors, offset_corrected, predict_all, station_wind,
)
from qc import mask_flatlines, pm_matrix, run_lengths
from windfield import build_wind_field

PROJECT_ROOT = Path(__file__).resolve().parent.parent
RAW_DIR = PROJECT_ROOT / "data" / "raw"
OUT_DIR = PROJECT_ROOT / "data" / "processed"

MAX_FILL_HOURS = 24  # the longest gap the backtest covered
DEFAULT_ERROR_PCT = 25.0  # used if the backtest file is absent


def expected_error_pct(backtest_path: Path = OUT_DIR / "gapfill_backtest.csv") -> float:
    """Typical absolute error of the neighbour fill as a % of the level, from the backtest."""
    if not backtest_path.exists():
        return DEFAULT_ERROR_PCT
    b = pd.read_csv(backtest_path)
    b = b[b["method"] == "neighbours"]
    if b.empty:
        return DEFAULT_ERROR_PCT
    return round(float((b["mae_ugm3"] / b["mean_truth_ugm3"]).mean() * 100), 0)


def interior_gap_runs(values: np.ndarray) -> list[tuple[int, int]]:
    """(start, length) of NaN runs that have a finite value on BOTH sides."""
    out = []
    for start, length in run_lengths(~np.isfinite(values)):
        if start > 0 and start + length < len(values):
            out.append((start, length))
    return out


def fill_gaps(raw: pd.DataFrame, clean: pd.DataFrame, pred_log: np.ndarray, max_fill_hours: int = MAX_FILL_HOURS,
              error_pct: float = DEFAULT_ERROR_PCT) -> pd.DataFrame:
    """Return the long-format filled series.

    raw:      (hours x stations) as measured, stuck sensors included
    clean:    same with stuck-sensor hours blanked (NaN)
    pred_log: (hours x stations) neighbour prediction in log space (leave-one-out)
    """
    logp = np.log(clean.to_numpy(float))
    corrected, _ = offset_corrected(logp, pred_log)
    estimate = np.exp(corrected)

    rows = []
    for j, station in enumerate(clean.columns):
        obs = clean.iloc[:, j].to_numpy(float)
        stuck = raw.iloc[:, j].notna().to_numpy() & ~np.isfinite(obs)
        fillable = np.zeros(len(obs), dtype=bool)
        for start, length in interior_gap_runs(obs):
            if length <= max_fill_hours:
                fillable[start:start + length] = True
        use = fillable & np.isfinite(estimate[:, j])

        final = np.where(np.isfinite(obs), obs, np.where(use, estimate[:, j], np.nan))
        source = np.full(len(obs), "unfilled", dtype=object)
        source[np.isfinite(obs)] = "observed"
        source[use & ~stuck] = "neighbour_fill"
        source[use & stuck] = "stuck_replaced"
        source[stuck & ~use] = "stuck_removed"
        measured = np.flatnonzero(np.isfinite(raw.iloc[:, j].to_numpy(float)))
        if len(measured) == 0:
            continue
        keep = np.zeros(len(obs), dtype=bool)
        keep[measured[0]:measured[-1] + 1] = True  # the station's record span: first to last measured hour
        for i in np.flatnonzero(keep):
            rows.append((clean.index[i], station, raw.iloc[i, j], final[i], source[i],
                         error_pct if source[i] in ("neighbour_fill", "stuck_replaced") else np.nan))
    out = pd.DataFrame(rows, columns=["hour_start_ist", "station", "pm25_measured", "pm25_final", "source", "expected_abs_error_pct"])
    out["pm25_final"] = out["pm25_final"].round(2)
    return out


def main() -> int:
    files = sorted(glob.glob(str(RAW_DIR / "openaq_hourly_*.csv")))
    cp = sorted(glob.glob(str(RAW_DIR / "cpcb_ncr_*.csv")))
    wf_files = sorted(glob.glob(str(RAW_DIR / "era5_wind_*.csv")))
    if not (files and cp and wf_files):
        print("Need data/raw openaq_hourly_*, cpcb_ncr_* and era5_wind_* files.")
        return 1
    openaq = pd.concat((pd.read_csv(f) for f in files), ignore_index=True)
    cpcb = pd.concat((pd.read_csv(f, usecols=["station", "latitude", "longitude"]) for f in cp), ignore_index=True)
    wind = build_wind_field(pd.concat((pd.read_csv(f) for f in wf_files), ignore_index=True))

    raw = pm_matrix(openaq)
    clean = mask_flatlines(raw)
    stations = list(clean.columns)
    coords = cpcb.dropna().drop_duplicates("station", keep="last")
    neighbors = build_neighbors(coords, stations)
    speed, wfrom = station_wind(clean.index, coords, stations, wind)
    pred = predict_all(np.log(clean.to_numpy(float)), neighbors, speed, wfrom)["knn_median"]

    err = expected_error_pct()
    out = fill_gaps(raw, clean, pred, error_pct=err)

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    out.to_csv(OUT_DIR / "pm25_hourly_filled.csv", index=False)

    counts = out["source"].value_counts()
    total_possible = int(raw.notna().sum().sum())
    print(f"{len(stations)} stations x {len(clean)} hours. Fill error assumed {err:.0f}% (from the backtest).")
    print(counts.to_string())
    n_obs, n_filled = int(counts.get("observed", 0)), int(counts.get("neighbour_fill", 0) + counts.get("stuck_replaced", 0))
    print(f"\nUsable hours: {n_obs} measured -> {n_obs + n_filled} after filling "
          f"(+{n_filled / max(n_obs, 1) * 100:.1f}%); {int(counts.get('stuck_replaced', 0) + counts.get('stuck_removed', 0))} stuck-sensor "
          f"hours handled; measured hours before QC: {total_possible}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
