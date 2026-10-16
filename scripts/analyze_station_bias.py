"""
Component 1: flag stations whose ground PM2.5 is persistently inconsistent with the
satellite (MODIS MAIAC AOD) picture of the same day.

Method (cross-sectional, per day):
  Each day, across all stations with both a ground reading and a valid AOD pixel, fit a
  robust line log(PM2.5) ~ log(AOD). That absorbs everything that moves the WHOLE city
  together (boundary-layer height, regional haze, humidity, retrieval bias), leaving
  only the spatial pattern. A station's residual is how far above/below the network
  trend it sat that day, in robust-sigma units. A station is flagged only if its
  residual is large, keeps the same sign on most days, and has enough days behind it.

Gate: if the satellite carries no usable signal (median daily rank correlation between
PM2.5 and AOD too low), NOTHING is flagged -- flagging on noise is worse than silence.

Limits to keep in mind: AOD is a column quantity at ~1 km; PM2.5 is a point reading at
the surface. A station near a local source (traffic, construction, a brick kiln)
legitimately reads high against the satellite, so a flag means "investigate", not
"faulty". True DRIFT (a slow ramp) needs weeks of history; the trend column is
exploratory until then.

FINDING (first real period, 2026-09-29..10-07): the cross-sectional method above is gated
OFF -- within a day, AOD and PM2.5 are uncorrelated across stations (median rho 0.03).
The signal is temporal: temporal_decoupling() compares each station's day-to-day
tracking of its own AOD pixel against its peers, with multiple-comparison control.

Usage:
    python scripts/analyze_station_bias.py
Writes data/processed/: station_day_residuals, station_bias_scores,
daily_satellite_signal, station_temporal_decoupling (all .csv).
"""

from __future__ import annotations

import glob
import sys
from pathlib import Path

import numpy as np
import pandas as pd
from scipy import stats
from qc import drop_flatlined_hours

PROJECT_ROOT = Path(__file__).resolve().parent.parent
RAW_DIR = PROJECT_ROOT / "data" / "raw"
OUT_DIR = PROJECT_ROOT / "data" / "processed"

# MODIS Terra passes ~10:30 and Aqua ~13:30 local time; MCD19A2 combines them.
SAT_WINDOW_HOURS = (10, 14)  # inclusive, IST
MIN_WINDOW_HOURS = 3
MIN_STATIONS_PER_DAY = 15
MIN_SIGNAL_RHO = 0.30  # median daily Spearman(PM2.5, AOD) below this -> no flags
MIN_DAYS_TO_FLAG = 7
FLAG_ABS_RESID_Z = 1.5
FLAG_SAME_SIGN_FRAC = 0.70
MIN_DAYS_FOR_TREND = 10
AOD_COLUMN = "aod_055"  # 550 nm is the standard reporting wavelength


def ground_window_daily(openaq_long: pd.DataFrame) -> pd.DataFrame:
    """Hourly OpenAQ (long) -> one PM2.5 value per station-day, averaged over the
    satellite overpass window. Requires MIN_WINDOW_HOURS hours so one stray hour
    can't stand in for the day."""
    pm = openaq_long[openaq_long["parameter"] == "pm25"].copy()
    pm["ts"] = pd.to_datetime(pm["hour_start_ist"].str[:19])
    pm["date"] = pm["ts"].dt.strftime("%Y-%m-%d")
    pm = pm[(pm["ts"].dt.hour >= SAT_WINDOW_HOURS[0]) & (pm["ts"].dt.hour <= SAT_WINDOW_HOURS[1])]
    g = pm.groupby(["station", "date"]).agg(pm25=("value", "mean"), n_hours=("value", "size")).reset_index()
    return g[g["n_hours"] >= MIN_WINDOW_HOURS].reset_index(drop=True)


def join_satellite(ground: pd.DataFrame, aod: pd.DataFrame) -> pd.DataFrame:
    sat = aod[["station", "date", AOD_COLUMN]].rename(columns={AOD_COLUMN: "aod"})
    sat = sat.drop_duplicates(["station", "date"], keep="last")
    df = ground.merge(sat, on=["station", "date"], how="inner")
    return df[(df["pm25"] > 0) & (df["aod"] > 0)].reset_index(drop=True)


def daily_residuals(joined: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Per-day robust cross-sectional fit. Returns (station-day residuals, per-day stats)."""
    rows, day_stats = [], []
    for date, g in joined.groupby("date"):
        if len(g) < MIN_STATIONS_PER_DAY:
            continue
        x, y = np.log(g["aod"].to_numpy()), np.log(g["pm25"].to_numpy())
        rho = stats.spearmanr(x, y).statistic
        slope, intercept, _, _ = stats.theilslopes(y, x)
        resid = y - (intercept + slope * x)
        sigma = 1.4826 * np.median(np.abs(resid - np.median(resid)))
        if not np.isfinite(sigma) or sigma == 0:
            continue
        z = (resid - np.median(resid)) / sigma
        out = g[["station", "date", "pm25", "aod"]].copy()
        out["resid_z"] = z
        rows.append(out)
        day_stats.append({"date": date, "n_stations": len(g), "spearman_rho": rho, "slope": slope})
    resid_df = pd.concat(rows, ignore_index=True) if rows else pd.DataFrame(columns=["station", "date", "pm25", "aod", "resid_z"])
    return resid_df, pd.DataFrame(day_stats, columns=["date", "n_stations", "spearman_rho", "slope"])


def signal_gate(day_stats: pd.DataFrame) -> tuple[bool, float]:
    if day_stats.empty:
        return False, float("nan")
    median_rho = float(day_stats["spearman_rho"].median())
    return median_rho >= MIN_SIGNAL_RHO, median_rho


def station_scores(resid_df: pd.DataFrame, signal_ok: bool) -> pd.DataFrame:
    cols = ["station", "n_days", "median_resid_z", "frac_same_sign", "trend_z_per_day", "flag", "reason"]
    if resid_df.empty:
        return pd.DataFrame(columns=cols)
    out = []
    for station, g in resid_df.sort_values("date").groupby("station"):
        n = len(g)
        med = float(g["resid_z"].median())
        same = float((np.sign(g["resid_z"]) == np.sign(med)).mean()) if med != 0 else 0.0
        trend = np.nan
        if n >= MIN_DAYS_FOR_TREND:
            day_idx = (pd.to_datetime(g["date"]) - pd.to_datetime(g["date"]).min()).dt.days.to_numpy()
            trend = float(stats.theilslopes(g["resid_z"].to_numpy(), day_idx)[0])

        if not signal_ok:
            flag, reason = False, "no flag: satellite signal too weak this period"
        elif n < MIN_DAYS_TO_FLAG:
            flag, reason = False, f"insufficient days ({n} < {MIN_DAYS_TO_FLAG})"
        elif abs(med) >= FLAG_ABS_RESID_Z and same >= FLAG_SAME_SIGN_FRAC:
            flag = True
            reason = ("reads HIGH" if med > 0 else "reads LOW") + " vs satellite, consistently"
        else:
            flag, reason = False, "consistent with network"
        out.append(
            {"station": station, "n_days": n, "median_resid_z": round(med, 2), "frac_same_sign": round(same, 2),
             "trend_z_per_day": None if np.isnan(trend) else round(trend, 3), "flag": flag, "reason": reason}
        )
    return pd.DataFrame(out, columns=cols).sort_values("median_resid_z", key=np.abs, ascending=False).reset_index(drop=True)


def temporal_decoupling(joined: pd.DataFrame, min_days: int = 8, fdr: float = 0.10) -> pd.DataFrame:
    """Does each station's PM2.5 follow the satellite's day-to-day pattern like its peers?

    This is the detector the data actually supports. Measured on the first real period
    (2026-09-29..10-07): across stations within a day, AOD and PM2.5 are uncorrelated
    (median rho 0.03) because AOD varies far less spatially than PM2.5 does, but across
    DAYS the typical station tracks its own AOD pixel (median rho 0.62). So compare each
    station's temporal Spearman rho against the network's.

    Significance: Fisher z-test of the station's rho against the network median (SE ~=
    1.06/sqrt(n-3) for Spearman), one-sided toward LOW, then Benjamini-Hochberg across
    stations. With ~9 days a single station's rho is very noisy; without the FDR step
    the lowest few of 50 stations would "look" decoupled by chance alone.
    """
    cols = ["station", "n_days", "rho", "network_median_rho", "z", "p_low", "q_value", "flag", "reason"]
    rows = []
    for station, g in joined.groupby("station"):
        if len(g) >= min_days:
            rho = stats.spearmanr(np.log(g["aod"]), np.log(g["pm25"])).statistic
            if np.isfinite(rho):
                rows.append((station, len(g), float(rho)))
    if not rows:
        return pd.DataFrame(columns=cols)

    df = pd.DataFrame(rows, columns=["station", "n_days", "rho"])
    center = float(df["rho"].median())
    clipped = lambda r: np.arctanh(np.clip(r, -0.999, 0.999))  # noqa: E731
    se = 1.06 / np.sqrt(df["n_days"] - 3)
    df["network_median_rho"] = round(center, 3)
    df["z"] = (clipped(df["rho"]) - clipped(center)) / se
    df["p_low"] = stats.norm.cdf(df["z"])

    # Benjamini-Hochberg
    order = df["p_low"].argsort().to_numpy()
    m = len(df)
    ranked = df["p_low"].to_numpy()[order] * m / np.arange(1, m + 1)
    q_sorted = np.minimum.accumulate(ranked[::-1])[::-1].clip(max=1.0)
    q = np.empty(m)
    q[order] = q_sorted
    df["q_value"] = q

    no_signal = bool(center < MIN_SIGNAL_RHO)
    flag = (df["q_value"] < fdr) & (df["rho"] < center) & (not no_signal)
    df["flag"] = flag
    # NB: never use `~` on a Python bool here (~True == -2, which is truthy).
    watch = (~flag) & (df["p_low"] < 0.05) & (df["rho"] < center)
    df["reason"] = np.where(
        no_signal, "no flag: network itself does not track the satellite",
        np.where(
            flag, "PM2.5 decoupled from satellite vs peers",
            np.where(watch, "watch: looks decoupled, not significant after multiple-comparison correction (needs more days)",
                     "tracks satellite like the network"),
        ),
    )
    for c in ("rho", "z", "p_low", "q_value"):
        df[c] = df[c].round(3)
    return df[cols].sort_values("p_low").reset_index(drop=True)


def _read_many(pattern: str) -> pd.DataFrame:
    files = sorted(glob.glob(str(RAW_DIR / pattern)))
    return pd.concat((pd.read_csv(f) for f in files), ignore_index=True) if files else pd.DataFrame()


def main() -> int:
    openaq, aod = _read_many("openaq_hourly_*.csv"), _read_many("aod_ncr_*.csv")
    if openaq.empty or aod.empty:
        print("Need both data/raw/openaq_hourly_*.csv and aod_ncr_*.csv -- run the fetchers/backfills first.")
        return 1

    openaq, stuck = drop_flatlined_hours(openaq)
    if len(stuck):
        print(f"QC: removed {int(stuck['hours_removed'].sum())} stuck-sensor station-hours "
              f"({stuck['station'].nunique()} stations) before analysis")
    joined = join_satellite(ground_window_daily(openaq), aod)
    resid_df, day_stats = daily_residuals(joined)
    ok, median_rho = signal_gate(day_stats)
    scores = station_scores(resid_df, ok)
    temporal = temporal_decoupling(joined)

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    temporal.to_csv(OUT_DIR / "station_temporal_decoupling.csv", index=False)
    resid_df.to_csv(OUT_DIR / "station_day_residuals.csv", index=False)
    scores.to_csv(OUT_DIR / "station_bias_scores.csv", index=False)
    day_stats.to_csv(OUT_DIR / "daily_satellite_signal.csv", index=False)

    print(f"Paired station-days: {len(joined)} over {joined['date'].nunique()} dates "
          f"({len(day_stats)} dates with >= {MIN_STATIONS_PER_DAY} stations)")
    print(f"Median daily Spearman(PM2.5, AOD): {median_rho:.2f}  (gate: >= {MIN_SIGNAL_RHO}) -> "
          f"{'signal OK' if ok else 'SIGNAL TOO WEAK, no flags raised'}")
    print(f"Cross-sectional detector flagged: {int(scores['flag'].sum())} of {len(scores)}")
    print()
    if temporal.empty:
        print("Temporal detector: no station has enough paired days yet.")
    else:
        print(f"Temporal detector: network median rho = {temporal['network_median_rho'].iloc[0]:.2f}; "
              f"flagged {int(temporal['flag'].sum())} of {len(temporal)} (BH-FDR q < 0.10). Lowest 8 by p:")
        print(temporal.head(8).to_string(index=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
