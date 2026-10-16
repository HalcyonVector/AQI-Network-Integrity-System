"""Shared data-quality helpers for the analyses.

Stuck sensors repeat one value for hours (seen on real data: JNU read exactly 1.00 ug/m3 for
8 h while its neighbours read 33; Vikas Sadan sat at 102 for 19 h while they read 52-83).
Hourly values are averages of 15-minute readings, so many consecutive hours that are EXACTLY
equal is not a plausible measurement.

Measured impact on the first real period: 111 of 21,303 station-hours (0.5%) are stuck. Removing
them barely changes the neighbour predictions (MAE 0.3296 -> 0.3299 with the median predictor,
because a median shrugs off one bad neighbour), so this is mainly hygiene: it stops a stuck value
from sitting inside that station's own daily means, and protects any NON-robust consumer (a mean
over neighbours is badly corrupted by one stuck station -- see the tests).
"""

from __future__ import annotations

import numpy as np
import pandas as pd

FLATLINE_MIN_HOURS = 6
PM_MAX = 1500.0  # above this a reading is treated as a glitch


def pm_matrix(openaq_long: pd.DataFrame, parameter: str = "pm25") -> pd.DataFrame:
    """Hourly values of one parameter as an (hours x stations) frame, IST hour start, on a complete
    hourly grid. Non-positive or implausibly large readings become NaN."""
    pm = openaq_long[openaq_long["parameter"] == parameter].copy()
    pm["ts"] = pd.to_datetime(pm["hour_start_ist"].str[:19])
    m = pm.pivot_table(index="ts", columns="station", values="value", aggfunc="mean")
    m = m.reindex(pd.date_range(m.index.min(), m.index.max(), freq="h"))
    return m.where((m > 0) & (m <= PM_MAX))


def run_lengths(mask: np.ndarray) -> list[tuple[int, int]]:
    """(start, length) of each run of True in a 1-D boolean array."""
    out, start = [], None
    for i, v in enumerate(mask):
        if v and start is None:
            start = i
        elif not v and start is not None:
            out.append((start, i - start))
            start = None
    if start is not None:
        out.append((start, len(mask) - start))
    return out


def flatline_runs(values: np.ndarray, min_hours: int = FLATLINE_MIN_HOURS) -> list[tuple[int, int]]:
    """Runs where the hourly value repeats EXACTLY (a stuck sensor), as (start, n_hours) counting the
    first hour of the run, with at least min_hours identical consecutive hours. NaNs never count."""
    same = np.zeros(len(values), dtype=bool)
    same[1:] = np.isfinite(values[1:]) & np.isfinite(values[:-1]) & (np.abs(values[1:] - values[:-1]) < 1e-9)
    runs = []
    for start, length in run_lengths(same):
        if length + 1 >= min_hours:
            runs.append((start - 1, length + 1))
    return runs


def flatline_mask(values: np.ndarray, min_hours: int = FLATLINE_MIN_HOURS) -> np.ndarray:
    """Boolean array, True for every hour inside a flatline run (including its first hour)."""
    mask = np.zeros(len(values), dtype=bool)
    for start, n in flatline_runs(values, min_hours):
        mask[start:start + n] = True
    return mask


def mask_flatlines(pm: pd.DataFrame, min_hours: int = FLATLINE_MIN_HOURS) -> pd.DataFrame:
    """Copy of an (hours x stations) frame with every flatlined hour set to NaN."""
    out = pm.copy()
    for col in out.columns:
        out.loc[flatline_mask(out[col].to_numpy(float), min_hours), col] = np.nan
    return out


def drop_flatlined_hours(
    openaq_long: pd.DataFrame, parameters: tuple[str, ...] = ("pm25", "pm10"), min_hours: int = FLATLINE_MIN_HOURS
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Remove stuck-sensor hours from the long-format OpenAQ data.

    Returns (cleaned long frame, report of hours removed per station and parameter). Other
    parameters (wind, humidity, gases) are untouched: a stuck PM sensor says nothing about them.
    """
    drop_idx, report = [], []
    for parameter in parameters:
        if not (openaq_long["parameter"] == parameter).any():
            continue
        flagged = mask_flatlines(pm_matrix(openaq_long, parameter), min_hours).isna() & pm_matrix(openaq_long, parameter).notna()
        for station in flagged.columns[flagged.any()]:
            hours = flagged.index[flagged[station].to_numpy()]
            report.append({"station": station, "parameter": parameter, "hours_removed": len(hours)})
            sel = (openaq_long["parameter"] == parameter) & (openaq_long["station"] == station)
            ts = pd.to_datetime(openaq_long.loc[sel, "hour_start_ist"].str[:19])
            drop_idx.extend(openaq_long.index[sel][ts.isin(hours).to_numpy()])
    cleaned = openaq_long.drop(index=drop_idx) if drop_idx else openaq_long
    return cleaned, pd.DataFrame(report, columns=["station", "parameter", "hours_removed"])
