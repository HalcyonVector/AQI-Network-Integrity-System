"""
Component 4: how much does regional stubble burning explain Delhi's PM2.5?

Screening-level method (not a dispersion model):
  1. Forward trajectories. Every VIIRS fire detection (FIRMS) releases an air parcel that
     is advected for up to 72 h along ERA5 10 m winds (1-hourly steps, linear in time,
     bilinear in space). A fire "hits" if the parcel passes within RECEPTOR_RADIUS_KM of
     Delhi; we record when. Fire radiative power (FRP) is the weight.
  2. Daily stubble-exposure index = FRP-weighted hits arriving at Delhi per IST day.
  3. Association with Delhi's daily mean PM2.5 (OpenAQ concentrations), judged against
     (a) a permutation null and (b) a wind-blind baseline (plain count of fires within
     400 km), and (c) partialled for Delhi's own wind speed -- because stagnant air both
     traps pollution and slows transport, which can fake a fire signal.

What this can and cannot say: it reports an EXPOSURE INDEX and how strongly it tracks PM2.5.
It does NOT output "X% of Delhi's PM2.5 is from stubble". That needs a calibrated
dispersion/chemistry model and boundary-layer data we do not have. Known simplifications:
10 m winds understate transport-layer winds (hence the speed-factor sensitivity), no
vertical mixing, no deposition, no plume rise, and a single regional receptor.

Usage:
    python scripts/analyze_stubble_transport.py
Writes data/processed/stubble_transport_daily.csv and stubble_association.csv.
"""

from __future__ import annotations

import glob
import sys
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd
from scipy import stats

PROJECT_ROOT = Path(__file__).resolve().parent.parent
RAW_DIR = PROJECT_ROOT / "data" / "raw"
OUT_DIR = PROJECT_ROOT / "data" / "processed"

DELHI = (28.61, 77.21)  # receptor centre (lat, lon)
RECEPTOR_RADIUS_KM = 60.0
MAX_TRAVEL_HOURS = 72
STEP_HOURS = 1.0
SPEED_FACTORS = (1.0, 1.5, 2.0)  # 10 m wind -> transport-layer wind sensitivity
NEAR_RADIUS_KM = 400.0  # baseline: fires within this distance of Delhi, wind ignored
KM_PER_DEG = 111.19
IST_OFFSET = pd.Timedelta(hours=5, minutes=30)
MIN_HOURS_PER_DAY = 18
N_PERMUTATIONS = 5000
MIN_DAYS_FOR_ASSOCIATION = 8
MAX_WIND_GAP = pd.Timedelta(hours=3, minutes=1)  # ERA5 is 3-hourly; refuse to bridge bigger holes


# ---------------------------------------------------------------- wind field


@dataclass
class WindField:
    times: np.ndarray  # datetime64[ns], ascending
    lats: np.ndarray  # ascending, uniform
    lons: np.ndarray  # ascending, uniform
    u: np.ndarray  # (time, lat, lon) m/s, NaN where missing
    v: np.ndarray


def build_wind_field(df: pd.DataFrame) -> WindField:
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
    u[ti, ai, oi] = df["u10_ms"].to_numpy()
    v[ti, ai, oi] = df["v10_ms"].to_numpy()
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


# ---------------------------------------------------------------- trajectories


def haversine_km(lat1, lon1, lat2, lon2):
    rad = np.pi / 180
    a = np.sin((lat2 - lat1) * rad / 2) ** 2 + np.cos(lat1 * rad) * np.cos(lat2 * rad) * np.sin((lon2 - lon1) * rad / 2) ** 2
    return 2 * 6371.0 * np.arcsin(np.sqrt(a))


def advect_fires(
    fires: pd.DataFrame,
    wind: WindField,
    receptor: tuple[float, float] = DELHI,
    radius_km: float = RECEPTOR_RADIUS_KM,
    speed_factor: float = 1.0,
    max_hours: int = MAX_TRAVEL_HOURS,
    step_hours: float = STEP_HOURS,
) -> pd.DataFrame:
    """Forward-advect each fire's parcel. `fires` needs fire_time, latitude, longitude, frp.

    Returns one row per fire: hit (parcel came within radius), travel_h (hours until it
    first did), arrival_time, local (the fire was ALREADY within the radius -- local
    burning, not transport), min_dist_km (closest approach while the parcel was alive).
    """
    n = len(fires)
    lat = fires["latitude"].to_numpy(dtype=float).copy()
    lon = fires["longitude"].to_numpy(dtype=float).copy()
    t0 = fires["fire_time"].to_numpy().astype("datetime64[ns]")
    alive = np.ones(n, dtype=bool)
    travel = np.full(n, np.nan)
    min_dist = np.full(n, np.inf)
    local = haversine_km(lat, lon, receptor[0], receptor[1]) <= radius_km
    dt_s = step_hours * 3600.0

    for k in range(int(max_hours / step_hours) + 1):
        h = k * step_hours
        dist = haversine_km(lat, lon, receptor[0], receptor[1])
        min_dist = np.where(alive, np.minimum(min_dist, dist), min_dist)
        newly = alive & (dist <= radius_km) & np.isnan(travel)
        travel[newly] = h
        if k == int(max_hours / step_hours) or not alive.any():
            break
        ids = np.flatnonzero(alive)
        t = t0[ids] + np.timedelta64(int(h * 3600), "s")
        u, v, ok = wind_at(wind, t, lat[ids], lon[ids])
        moved = ids[ok]
        lat[moved] += v[ok] * speed_factor * dt_s / (KM_PER_DEG * 1000)
        lon[moved] += u[ok] * speed_factor * dt_s / (KM_PER_DEG * 1000 * np.cos(np.radians(lat[moved])))
        alive[ids[~ok]] = False  # left the wind coverage (space or time)

    out = fires[["fire_time", "latitude", "longitude", "frp"]].copy()
    out["hit"] = ~np.isnan(travel)
    out["travel_h"] = travel
    out["arrival_time"] = out["fire_time"] + pd.to_timedelta(np.nan_to_num(travel, nan=0.0), unit="h")
    out.loc[~out["hit"], "arrival_time"] = pd.NaT
    out["local"] = local
    out["min_dist_km"] = np.where(np.isfinite(min_dist), min_dist, np.nan)
    return out.reset_index(drop=True)


def ist_date(ts: pd.Series) -> pd.Series:
    return (ts + IST_OFFSET).dt.strftime("%Y-%m-%d")


def daily_exposure(traj: pd.DataFrame) -> pd.DataFrame:
    """FRP-weighted TRANSPORT hits per IST arrival day. Local fires (already inside the
    receptor) are excluded: they are not regional transport."""
    h = traj[traj["hit"] & ~traj["local"]].copy()
    if h.empty:
        return pd.DataFrame(columns=["date", "hit_fires", "hit_frp"])
    h["date"] = ist_date(h["arrival_time"])
    return h.groupby("date").agg(hit_fires=("frp", "size"), hit_frp=("frp", "sum")).reset_index()


# ---------------------------------------------------------------- association statistics


def permutation_spearman(x: np.ndarray, y: np.ndarray, n_perm: int = N_PERMUTATIONS, seed: int = 0) -> tuple[float, float]:
    """Spearman rho and a two-sided permutation p-value (shuffle x against y)."""
    rho = stats.spearmanr(x, y).statistic
    if not np.isfinite(rho):
        return float("nan"), float("nan")
    rng = np.random.default_rng(seed)
    rx, ry = stats.rankdata(x), stats.rankdata(y)
    null = np.array([np.corrcoef(rng.permutation(rx), ry)[0, 1] for _ in range(n_perm)])
    p = (1 + np.sum(np.abs(null) >= abs(rho) - 1e-12)) / (n_perm + 1)
    return float(rho), float(p)


def circular_shift_p(x: np.ndarray, y: np.ndarray) -> tuple[float, float]:
    """Spearman rho with a p-value from circularly SHIFTING x against y.

    Daily series are strongly autocorrelated (and here dominated by one regime change), so
    shuffling days independently overstates significance. Circular shifts keep each
    series' own autocorrelation and break only the alignment between them. With n days
    there are only n-1 non-trivial shifts, so the smallest attainable p is about 1/n --
    at n = 13 this test CANNOT reach p < 0.05, which is itself the honest message.
    """
    n = len(x)
    rho = stats.spearmanr(x, y).statistic
    if n < 4 or not np.isfinite(rho):
        return float("nan"), float("nan")
    null = [stats.spearmanr(np.roll(x, s), y).statistic for s in range(1, n)]
    null = np.array([r for r in null if np.isfinite(r)])
    return float(rho), float((1 + np.sum(np.abs(null) >= abs(rho) - 1e-12)) / (len(null) + 1))


def partial_spearman(x: np.ndarray, y: np.ndarray, z: np.ndarray, n_perm: int = N_PERMUTATIONS, seed: int = 0) -> tuple[float, float]:
    """Spearman correlation of x and y after removing the (rank-linear) effect of z from
    both, with a permutation p-value on the x residuals."""
    rx, ry, rz = (stats.rankdata(a) for a in (x, y, z))

    def resid(a):
        slope, intercept = np.polyfit(rz, a, 1)
        return a - (slope * rz + intercept)

    ex, ey = resid(rx), resid(ry)
    if np.std(ex) == 0 or np.std(ey) == 0:
        return float("nan"), float("nan")
    rho = float(np.corrcoef(ex, ey)[0, 1])
    rng = np.random.default_rng(seed)
    null = np.array([np.corrcoef(rng.permutation(ex), ey)[0, 1] for _ in range(n_perm)])
    return rho, float((1 + np.sum(np.abs(null) >= abs(rho) - 1e-12)) / (n_perm + 1))


# ---------------------------------------------------------------- data loading


def _read_many(pattern: str, **kw) -> pd.DataFrame:
    files = sorted(glob.glob(str(RAW_DIR / pattern)))
    return pd.concat((pd.read_csv(f, **kw) for f in files), ignore_index=True) if files else pd.DataFrame()


def load_fires() -> pd.DataFrame:
    f = _read_many("firms_acq_*.csv", dtype={"acq_time": str})
    if f.empty:
        return f
    f["acq_time"] = f["acq_time"].str.zfill(4)
    f["fire_time"] = pd.to_datetime(f["acq_date"] + " " + f["acq_time"].str[:2] + ":" + f["acq_time"].str[2:], utc=True).dt.tz_localize(None)
    f = f[f["confidence"].astype(str).str.lower() != "l"]  # VIIRS: drop low-confidence detections
    return f.drop_duplicates(["source", "latitude", "longitude", "acq_date", "acq_time"]).reset_index(drop=True)


def delhi_daily_pm_and_wind(openaq: pd.DataFrame, station_city: dict[str, str]) -> pd.DataFrame:
    """Delhi-wide daily mean PM2.5 and station-measured wind speed (IST days with >= 18 hours)."""
    o = openaq[openaq["station"].map(station_city) == "Delhi"].copy()
    o["ts"] = pd.to_datetime(o["hour_start_ist"].str[:19])
    o["date"] = o["ts"].dt.strftime("%Y-%m-%d")
    out = []
    for param, col in (("pm25", "pm25_delhi"), ("wind_speed", "wind_delhi")):
        p = o[o["parameter"] == param]
        hourly = p.groupby("ts")["value"].mean().reset_index()
        hourly["date"] = hourly["ts"].dt.strftime("%Y-%m-%d")
        d = hourly.groupby("date").agg(**{col: ("value", "mean"), "hrs": ("value", "size")}).reset_index()
        d = d[d["hrs"] >= MIN_HOURS_PER_DAY].drop(columns="hrs")
        out.append(d)
    return out[0].merge(out[1], on="date", how="left")


def baseline_fire_counts(fires: pd.DataFrame) -> pd.DataFrame:
    near = haversine_km(fires["latitude"].to_numpy(), fires["longitude"].to_numpy(), DELHI[0], DELHI[1]) <= NEAR_RADIUS_KM
    f = fires.copy()
    f["date"] = ist_date(f["fire_time"])
    f["near"] = near
    return f.groupby("date").agg(fires_region=("frp", "size"), fires_near400=("near", "sum"), frp_region=("frp", "sum")).reset_index()


def valid_exposure_days(wind: WindField, max_hours: int = MAX_TRAVEL_HOURS) -> tuple[str, str]:
    """First and last IST dates whose arriving-parcel exposure is trustworthy.

    First: a parcel arriving on day D may have been launched up to `max_hours` earlier, so
    the wind record must start at least that long before D begins -- otherwise fires
    launched before the record are silently dropped and early days look falsely clean.
    Last: day D must END at or before the last wind timestamp (parcels have no wind
    beyond it, so later arrivals are unobservable, not absent).
    """
    w0, w1 = pd.Timestamp(wind.times[0]), pd.Timestamp(wind.times[-1])
    first = (w0 + pd.Timedelta(hours=max_hours) + IST_OFFSET).ceil("D")
    last = ((w1 + IST_OFFSET) - pd.Timedelta(days=1)).floor("D")
    return first.strftime("%Y-%m-%d"), last.strftime("%Y-%m-%d")


def association_table(daily: pd.DataFrame, exposure_cols: list[str]) -> pd.DataFrame:
    rows = []
    for col in exposure_cols:
        for lag in (0, 1):
            d = daily.copy()
            d["x"] = d[col].shift(lag)  # exposure that arrived `lag` days before the PM reading
            d = d.dropna(subset=["x", "pm25_delhi"])
            if len(d) < MIN_DAYS_FOR_ASSOCIATION:
                continue
            rho, p = permutation_spearman(d["x"].to_numpy(), d["pm25_delhi"].to_numpy())
            prho = pp = np.nan
            dd = d.dropna(subset=["wind_delhi"])
            if len(dd) >= MIN_DAYS_FOR_ASSOCIATION:
                prho, pp = partial_spearman(dd["x"].to_numpy(), dd["pm25_delhi"].to_numpy(), dd["wind_delhi"].to_numpy())
            _, cp = circular_shift_p(d["x"].to_numpy(), d["pm25_delhi"].to_numpy())
            # Does the AMOUNT of exposure matter, or only whether any arrived? Restrict to
            # days with exposure > 0 (removes the clean-vs-dirty step).
            ex = d[d["x"] > 0]
            erho = stats.spearmanr(ex["x"], ex["pm25_delhi"]).statistic if len(ex) >= 6 else np.nan
            rows.append({"exposure": col, "lag_days": lag, "n_days": len(d), "spearman": round(rho, 3), "perm_p": round(p, 4),
                         "circ_shift_p": round(cp, 3), "n_exposed_days": len(ex),
                         "spearman_exposed_days_only": None if not np.isfinite(erho) else round(float(erho), 3),
                         "partial_spearman_ctrl_wind": None if np.isnan(prho) else round(prho, 3),
                         "partial_perm_p": None if np.isnan(pp) else round(pp, 4)})
    return pd.DataFrame(rows)


def main() -> int:
    fires, wind_raw, openaq = load_fires(), _read_many("era5_wind_*.csv"), _read_many("openaq_hourly_*.csv")
    if fires.empty or wind_raw.empty or openaq.empty:
        print("Need data/raw firms_acq_*, era5_wind_* and openaq_hourly_* files.")
        return 1
    wind = build_wind_field(wind_raw)
    station_city = {}
    for f in sorted(glob.glob(str(RAW_DIR / "cpcb_ncr_*.csv"))):
        d = pd.read_csv(f, usecols=["station", "city"])
        station_city.update(dict(zip(d["station"], d["city"])))

    wind_end = pd.Timestamp(wind.times[-1])
    print(f"Fires: {len(fires)} detections {fires['acq_date'].min()}..{fires['acq_date'].max()} | "
          f"wind {pd.Timestamp(wind.times[0]):%Y-%m-%d %H:%M}..{wind_end:%Y-%m-%d %H:%M} UTC")

    daily = baseline_fire_counts(fires).merge(delhi_daily_pm_and_wind(openaq, station_city), on="date", how="outer")
    exposure_cols = []
    traj_summary = []
    for k in SPEED_FACTORS:
        traj = advect_fires(fires, wind, speed_factor=k)
        e = daily_exposure(traj).rename(columns={"hit_fires": f"hit_fires_k{k}", "hit_frp": f"hit_frp_k{k}"})
        daily = daily.merge(e, on="date", how="left")
        for c in (f"hit_fires_k{k}", f"hit_frp_k{k}"):
            daily[c] = daily[c].fillna(0.0)
        exposure_cols.append(f"hit_frp_k{k}")
        h = traj[traj["hit"] & ~traj["local"]]
        traj_summary.append((k, int(traj["local"].sum()), len(h), float(h["travel_h"].median()) if len(h) else np.nan))
    daily = daily.sort_values("date").reset_index(drop=True)

    first_day, last_day = valid_exposure_days(wind)
    analysis = daily[(daily["date"] >= first_day) & (daily["date"] <= last_day)].copy()
    analysis["fires_near400_f"] = analysis["fires_near400"].astype(float)

    assoc = association_table(analysis, ["fires_near400_f"] + exposure_cols)

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    daily.to_csv(OUT_DIR / "stubble_transport_daily.csv", index=False)
    assoc.to_csv(OUT_DIR / "stubble_association.csv", index=False)

    print("\nTrajectory summary (speed factor, local fires excluded, transport hits, median travel h):")
    for k, n_local, n_hit, med in traj_summary:
        print(f"  k={k}: {n_local} fires already within {RECEPTOR_RADIUS_KM:.0f} km of Delhi | {n_hit} transport hits | median {med:.0f} h")
    print(f"\nDays with Delhi PM2.5 and exposure: {int(analysis['pm25_delhi'].notna().sum())}  (wind record limits analysis to <= "
          f"{(wind_end + IST_OFFSET - pd.Timedelta(days=1)):%Y-%m-%d}; parcels need up to {MAX_TRAVEL_HOURS} h of wind after launch)")
    show = ["date", "fires_near400", "hit_fires_k1.5", "hit_frp_k1.5", "pm25_delhi", "wind_delhi"]
    print(analysis[show].dropna(subset=["pm25_delhi"]).round(1).to_string(index=False))
    print("\nAssociation with Delhi daily PM2.5 (permutation p, 5000 shuffles):")
    print(assoc.to_string(index=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
