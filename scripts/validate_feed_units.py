"""
What do the data.gov.in feed's pollutant columns actually contain?

The README first called them concentrations (an assumption); a one-station check suggested they are
CPCB AQI SUB-INDEX values of a trailing average. This tests that properly, across every pollutant and
every station, using OpenAQ (true concentrations) over the dates where both exist.

For each overlapping (station, hour, pollutant) it compares the feed value with
  H1  the CPCB sub-index of the trailing statistic (24 h mean for PM2.5/PM10/NO2/SO2; max 8 h mean
      within the last 24 h for ozone and CO), per the National AQI breakpoint tables, and
  H2  the raw concentration (trailing 24 h mean, and the 1 h value).
Whichever fits is what the feed carries. Also tries a one-hour shift of the window, since it is not
documented whether `last_update` labels the start or the end of the hour.

Usage:
    python scripts/validate_feed_units.py
Writes data/processed/feed_unit_validation.csv.
"""

from __future__ import annotations

import glob
import sys
from pathlib import Path

import numpy as np
import pandas as pd
from scipy import stats

from aqi_units import BREAKPOINTS, STATISTIC, concentration_from_subindex, sub_index  # noqa: F401
from qc import pm_matrix

PROJECT_ROOT = Path(__file__).resolve().parent.parent
RAW_DIR = PROJECT_ROOT / "data" / "raw"
OUT_DIR = PROJECT_ROOT / "data" / "processed"

# feed column -> (OpenAQ parameter, factor to the AQI table's unit, candidate trailing statistics).
# NO2 and SO2: OpenAQ labels them "ppb", but the feed only matches the AQI tables with NO unit
# conversion (first try with x1.88 / x2.62: feed medians 31 and 12 vs sub-index 58 and 32 -- but 24.5 and
# 9.75 straight through the tables give 30.6 and 12.2). The numbers are in CPCB's native ug/m3; the label
# is wrong, as it is for CO (mg/m3). Ozone and CO list two plausible averaging definitions (the CPCB
# documentation is ambiguous about whether the hourly value is the current 8 h mean or its 24 h maximum);
# both are reported and the better one is simply what the feed does.
POLLUTANTS = {
    "PM2.5": ("pm25", 1.0, ["mean24"]),
    "PM10": ("pm10", 1.0, ["mean24"]),
    "NO2": ("no2", 1.0, ["mean24"]),
    "SO2": ("so2", 1.0, ["mean24"]),
    "OZONE": ("o3", 1.0, ["max8", "mean8"]),
    "CO": ("co", 1.0, ["max8", "mean8"]),
}


def trailing_stat(hourly: pd.DataFrame, kind: str) -> pd.DataFrame:
    """Per-hour trailing statistic of an (hours x stations) frame.
    mean24: mean of the last 24 h (needs >= 16). max8: the maximum, within the last 24 h, of the
    8-hour rolling mean (each needs >= 6 of 8; the max needs >= 16 valid windows)."""
    if kind == "mean24":
        return hourly.rolling(24, min_periods=16).mean()
    if kind == "max8":
        return hourly.rolling(8, min_periods=6).mean().rolling(24, min_periods=16).max()
    if kind == "mean8":
        return hourly.rolling(8, min_periods=6).mean()
    raise ValueError(kind)


def compare(feed: pd.DataFrame, openaq: pd.DataFrame, shift_hours: int = 0) -> pd.DataFrame:
    """Long frame: station, hour, pollutant, stat_kind, feed, h1_subindex, h2_mean24, h2_hour for every
    feed reading that OpenAQ can reproduce. `shift_hours` moves the OpenAQ window (negative = earlier)."""
    rows = []
    for pollutant, (param, factor, kinds) in POLLUTANTS.items():
        if not (openaq["parameter"] == param).any() or pollutant not in feed.columns:
            continue
        hourly = pm_matrix(openaq, param) * factor
        mean24 = trailing_stat(hourly, "mean24").shift(shift_hours, freq="h")
        hour = hourly.shift(shift_hours, freq="h")
        for kind in kinds:
            stat = trailing_stat(hourly, kind).shift(shift_hours, freq="h")
            for (station, ts), value in feed.set_index(["station", "hour"])[pollutant].dropna().items():
                if station in stat.columns and ts in stat.index:
                    s_, m, h = stat.at[ts, station], mean24.at[ts, station], hour.at[ts, station]
                    if np.isfinite(s_):
                        rows.append((station, ts, pollutant, kind, float(value), float(sub_index(pollutant, s_)),
                                     float(m) if np.isfinite(m) else np.nan, float(h) if np.isfinite(h) else np.nan, float(s_)))
    return pd.DataFrame(rows, columns=["station", "hour", "pollutant", "stat_kind", "feed", "h1_subindex", "h2_mean24", "h2_hour", "stat_conc"])


def summarize(cmp: pd.DataFrame) -> pd.DataFrame:
    out = []
    for (pollutant, kind), g in cmp.groupby(["pollutant", "stat_kind"]):
        g = g.dropna(subset=["feed", "h1_subindex"])
        if len(g) < 10:
            continue
        e1 = (g["feed"] - g["h1_subindex"]).abs()
        g2 = g.dropna(subset=["h2_mean24"])
        e2 = (g2["feed"] - g2["h2_mean24"]).abs()
        out.append({
            "pollutant": pollutant, "statistic": kind, "n": len(g),
            "h1_spearman": round(float(stats.spearmanr(g["feed"], g["h1_subindex"]).statistic), 3),
            "h1_mae_points": round(float(e1.mean()), 2), "h1_within_5": round(float((e1 <= 5).mean()), 3),
            "h2_spearman": round(float(stats.spearmanr(g2["feed"], g2["h2_mean24"]).statistic), 3) if len(g2) > 10 else np.nan,
            "h2_mae_units": round(float(e2.mean()), 2) if len(g2) else np.nan,
            "feed_median": round(float(g["feed"].median()), 1), "h1_median": round(float(g["h1_subindex"].median()), 1),
            "conc_mean24_median": round(float(g2["h2_mean24"].median()), 2) if len(g2) else np.nan,
        })
    return pd.DataFrame(out)


def inversion_error(cmp: pd.DataFrame) -> pd.DataFrame:
    """How well does INVERTING the feed's sub-index recover the true concentration (OpenAQ)?
    Uses, per pollutant, the averaging window the feed was shown to use (aqi_units.STATISTIC)."""
    rows = []
    for pollutant, kind in STATISTIC.items():
        g = cmp[(cmp["pollutant"] == pollutant) & (cmp["stat_kind"] == kind)].dropna(subset=["feed", "stat_conc"])
        if len(g) < 10:
            continue
        inv = concentration_from_subindex(pollutant, g["feed"].to_numpy())
        err = np.abs(inv - g["stat_conc"].to_numpy())
        rel = err / np.maximum(g["stat_conc"].to_numpy(), 1e-9)
        rows.append({"pollutant": pollutant, "n": len(g), "mae_conc": round(float(err.mean()), 3),
                     "median_abs_err": round(float(np.median(err)), 3), "median_rel_err_pct": round(float(np.median(rel) * 100), 1),
                     "p90_rel_err_pct": round(float(np.quantile(rel, 0.9) * 100), 1),
                     "typical_conc": round(float(g["stat_conc"].median()), 2)})
    return pd.DataFrame(rows)


def load_feed(raw_dir: Path = RAW_DIR) -> pd.DataFrame:
    frames = [pd.read_csv(f) for f in sorted(glob.glob(str(raw_dir / "cpcb_ncr_*.csv")))]
    feed = pd.concat(frames, ignore_index=True)
    feed["station"] = feed["station"].str.strip()
    feed["hour"] = pd.to_datetime(feed["last_update"], format="%d-%m-%Y %H:%M:%S").dt.floor("h")
    return feed.drop_duplicates(["station", "hour"], keep="last")


def main() -> int:
    files = sorted(glob.glob(str(RAW_DIR / "openaq_hourly_*.csv")))
    if not files or not glob.glob(str(RAW_DIR / "cpcb_ncr_*.csv")):
        print("Need data/raw openaq_hourly_* and cpcb_ncr_* files.")
        return 1
    openaq = pd.concat((pd.read_csv(f) for f in files), ignore_index=True)
    openaq["station"] = openaq["station"].str.strip()
    feed = load_feed()
    print(f"feed readings: {len(feed)} | OpenAQ hours: {openaq['hour_start_ist'].str[:10].nunique()} dates")

    best = None
    for shift in (0, -1):
        cmp = compare(feed, openaq, shift_hours=shift)
        summ = summarize(cmp)
        if summ.empty:
            continue
        print(f"\n--- OpenAQ window shifted {shift:+d} h ---")
        print(summ.to_string(index=False))
        score = summ["h1_mae_points"].mean()
        if best is None or score < best[0]:
            best = (score, shift, cmp, summ)
    if best is None:
        print("No overlapping readings.")
        return 1
    inv = inversion_error(best[2])
    print("\nInverting the feed's sub-index back to concentration, vs OpenAQ (units: ug/m3, CO mg/m3):")
    print(inv.to_string(index=False))
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    best[3].to_csv(OUT_DIR / "feed_unit_validation.csv", index=False)
    inv.to_csv(OUT_DIR / "feed_inversion_error.csv", index=False)
    print(f"\nBest alignment: shift {best[1]:+d} h (mean H1 error {best[0]:.1f} index points). Written to feed_unit_validation.csv")
    return 0


if __name__ == "__main__":
    sys.exit(main())
