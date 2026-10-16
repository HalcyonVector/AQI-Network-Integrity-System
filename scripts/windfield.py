"""Shared ERA5 wind-field helpers (interpolation) and great-circle distance.

Used by the stubble-transport and neighbour-consistency analyses.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

MAX_WIND_GAP = pd.Timedelta(hours=3, minutes=1)  # ERA5 is 3-hourly; refuse to bridge bigger holes


@dataclass
class WindField:
    times: np.ndarray  # datetime64[ns], ascending
    lats: np.ndarray  # ascending, uniform
    lons: np.ndarray  # ascending, uniform
    u: np.ndarray  # (time, lat, lon) m/s, NaN where missing
    v: np.ndarray


def build_wind_field(df: pd.DataFrame, u_col: str = "u10_ms", v_col: str = "v10_ms") -> WindField:
    df = df.drop_duplicates(["time", "latitude", "longitude"]).copy()
    df["time"] = pd.to_datetime(df["time"])
    times = np.sort(df["time"].unique())
    lats = np.sort(df["latitude"].unique())
    lons = np.sort(df["longitude"].unique())
    for name, axis in (("latitude", lats), ("longitude", lons)):
        if len(axis) > 1 and not np.allclose(np.diff(axis), np.diff(axis)[0], atol=1e-6):
            raise ValueError(f"wind grid {name} is not uniformly spaced")
    ti = np.searchsorted(times, df["time"].to_numpy())
    ai = np.searchsorted(lats, df["latitude"].to_numpy())
    oi = np.searchsorted(lons, df["longitude"].to_numpy())
    u = np.full((len(times), len(lats), len(lons)), np.nan)
    v = np.full_like(u, np.nan)
    u[ti, ai, oi] = df[u_col].to_numpy()
    v[ti, ai, oi] = df[v_col].to_numpy()
    return WindField(times, lats, lons, u, v)


def wind_at(wind: WindField, t: np.ndarray, lat: np.ndarray, lon: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Trilinear (time, lat, lon) interpolation. Returns (u, v, valid); valid is False
    outside the time/space coverage, across a time gap bigger than MAX_WIND_GAP, or
    where any of the 8 surrounding values is missing."""
    n = len(lat)
    times = wind.times.astype("datetime64[ns]").astype(np.int64)
    ti = t.astype("datetime64[ns]").astype(np.int64)
    nt = len(times)

    idx = np.searchsorted(times, ti, side="right")
    in_time = (ti >= times[0]) & (ti <= times[-1])
    i1 = np.clip(idx, 0, nt - 1)
    i0 = np.clip(idx - 1, 0, nt - 1)
    span = (times[i1] - times[i0]).astype(float)
    wt = np.where(span > 0, (ti - times[i0]) / np.where(span > 0, span, 1), 0.0)
    gap_ok = span <= MAX_WIND_GAP.value

    dlat = wind.lats[1] - wind.lats[0]
    dlon = wind.lons[1] - wind.lons[0]
    fa = (lat - wind.lats[0]) / dlat
    fo = (lon - wind.lons[0]) / dlon
    in_space = (fa >= 0) & (fa <= len(wind.lats) - 1) & (fo >= 0) & (fo <= len(wind.lons) - 1)
    a0 = np.clip(np.floor(fa).astype(int), 0, len(wind.lats) - 2)
    o0 = np.clip(np.floor(fo).astype(int), 0, len(wind.lons) - 2)
    wa, wo = fa - a0, fo - o0

    def spatial(field, ti_idx):
        return (
            field[ti_idx, a0, o0] * (1 - wa) * (1 - wo)
            + field[ti_idx, a0 + 1, o0] * wa * (1 - wo)
            + field[ti_idx, a0, o0 + 1] * (1 - wa) * wo
            + field[ti_idx, a0 + 1, o0 + 1] * wa * wo
        )

    u = spatial(wind.u, i0) * (1 - wt) + spatial(wind.u, i1) * wt
    v = spatial(wind.v, i0) * (1 - wt) + spatial(wind.v, i1) * wt
    valid = in_time & gap_ok & in_space & np.isfinite(u) & np.isfinite(v)
    return np.where(valid, u, 0.0), np.where(valid, v, 0.0), valid




def haversine_km(lat1, lon1, lat2, lon2):
    rad = np.pi / 180
    a = np.sin((lat2 - lat1) * rad / 2) ** 2 + np.cos(lat1 * rad) * np.cos(lat2 * rad) * np.sin((lon2 - lon1) * rad / 2) ** 2
    return 2 * 6371.0 * np.arcsin(np.sqrt(a))
