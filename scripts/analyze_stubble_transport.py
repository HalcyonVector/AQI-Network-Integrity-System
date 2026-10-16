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
from pathlib import Path

import numpy as np
import pandas as pd
from scipy import stats
from qc import drop_flatlined_hours
from windfield import WindField, build_wind_field, haversine_km, wind_at  # noqa: F401

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
# A constant series ranks to constant values, which the regression intercept then explains almost
# perfectly -- leaving ~1e-15 of floating-point noise rather than exactly zero. Testing `std == 0`
# missed that and returned a garbage correlation instead of NaN.
DEGENERATE_STD = 1e-9
MIN_DAYS_FOR_ASSOCIATION = 8


# ---------------------------------------------------------------- trajectories


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


def _partial_residuals(x, y, z):
    """Rank-transform x, y and each column of z, then remove the linear effect of z (with intercept)
    from both ranks. z may be 1-D (one covariate) or 2-D (n x k)."""
    z = np.asarray(z, float)
    if z.ndim == 1:
        z = z[:, None]
    rz = np.column_stack([stats.rankdata(z[:, c]) for c in range(z.shape[1])])
    design = np.column_stack([np.ones(len(rz)), rz])

    def resid(a):
        ra = stats.rankdata(a)
        beta, *_ = np.linalg.lstsq(design, ra, rcond=None)
        return ra - design @ beta

    return resid(x), resid(y)


def partial_spearman(x: np.ndarray, y: np.ndarray, z: np.ndarray, n_perm: int = N_PERMUTATIONS, seed: int = 0) -> tuple[float, float]:
    """Spearman correlation of x and y after removing the (rank-linear) effect of one or more
    covariates z from both, with a permutation p-value on the x residuals.

    LIMIT: the control is rank-LINEAR. If x and y share a nonlinear dependence on z, a residual
    shared component survives and the partial correlation overstates what is left once z is
    accounted for (see tests). With ~15 days there is no room for a more flexible control.
    """
    ex, ey = _partial_residuals(x, y, z)
    if np.std(ex) < DEGENERATE_STD or np.std(ey) < DEGENERATE_STD:
        return float("nan"), float("nan")
    rho = float(np.corrcoef(ex, ey)[0, 1])
    rng = np.random.default_rng(seed)
    null = np.array([np.corrcoef(rng.permutation(ex), ey)[0, 1] for _ in range(n_perm)])
    return rho, float((1 + np.sum(np.abs(null) >= abs(rho) - 1e-12)) / (n_perm + 1))


def partial_circular_shift_p(x: np.ndarray, y: np.ndarray, z: np.ndarray) -> tuple[float, float]:
    """Like circular_shift_p, on the residuals after removing z: preserves each series' own
    autocorrelation. The smallest attainable p is about 1/n."""
    ex, ey = _partial_residuals(x, y, z)
    n = len(ex)
    if n < 4 or np.std(ex) < DEGENERATE_STD or np.std(ey) < DEGENERATE_STD:
        return float("nan"), float("nan")
    rho = float(np.corrcoef(ex, ey)[0, 1])
    null = np.array([np.corrcoef(np.roll(ex, k), ey)[0, 1] for k in range(1, n)])
    return rho, float((1 + np.sum(np.abs(null) >= abs(rho) - 1e-12)) / (len(null) + 1))


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


def delhi_daily_met(era5: pd.DataFrame, min_steps: int = 7) -> pd.DataFrame:
    """Daily (IST) boundary-layer height and temperature over the Delhi area, from ERA5.

    blh_delhi_max: the day's highest boundary-layer height of the area-mean series, i.e. the
                   afternoon mixing depth that sets how much a day can dilute pollution. It is the
                   confounder the first attribution pass could not control for.
    t2m_delhi:     the day's mean 2 m temperature, a proxy for the post-monsoon cooling that arrives
                   over the same weeks as the burning season.
    Averages the ERA5 cells within RECEPTOR_RADIUS_KM of Delhi; a day needs >= min_steps of its 8
    three-hourly timestamps.
    """
    cols = {"time", "latitude", "longitude", "blh_m", "t2m_k"}
    if not cols <= set(era5.columns):
        return pd.DataFrame(columns=["date", "blh_delhi_max", "t2m_delhi"])
    cells = era5.drop_duplicates(["latitude", "longitude"])[["latitude", "longitude"]]
    near = cells[haversine_km(cells["latitude"].to_numpy(), cells["longitude"].to_numpy(), DELHI[0], DELHI[1]) <= RECEPTOR_RADIUS_KM]
    e = era5.merge(near, on=["latitude", "longitude"])
    e["time"] = pd.to_datetime(e["time"])
    by_time = e.groupby("time").agg(blh=("blh_m", "mean"), t2m=("t2m_k", "mean")).reset_index()
    by_time["date"] = (by_time["time"] + IST_OFFSET).dt.strftime("%Y-%m-%d")
    out = by_time.groupby("date").agg(blh_delhi_max=("blh", "max"), t2m_delhi=("t2m", "mean"), steps=("blh", "size")).reset_index()
    return out[out["steps"] >= min_steps].drop(columns="steps")


RAIN_DAY_MM = 1.0  # a day with at least this much area-mean rain is a "wet" day
WASHOUT_DAYS = 2  # ...and PM2.5 is treated as still suppressed on the day itself and this many days after


def delhi_daily_precip(hourly: pd.DataFrame) -> pd.DataFrame:
    """Daily (IST) rain over Delhi from ERA5 hourly files: area-mean total in mm, and `washout`, True on a
    wet day (>= RAIN_DAY_MM) and the WASHOUT_DAYS days after it, since rain scrubs particulates and the
    effect lingers. ERA5 labels each hour with its END, so the value at 06:00 UTC is 05:00-06:00 UTC; an
    IST day D therefore owns the hours ending in (D 00:00 IST, D+1 00:00 IST], and a day needs all 24."""
    if hourly.empty or "precip_mm_delhi_mean" not in hourly:
        return pd.DataFrame(columns=["date", "precip_delhi_mm", "washout"])
    h = hourly.copy()
    h["time"] = pd.to_datetime(h["time"])
    ist = h["time"] + IST_OFFSET
    h["date"] = (ist - pd.Timedelta(minutes=1)).dt.strftime("%Y-%m-%d")  # an hour ending exactly at 00:00 IST belongs to the day it closes
    d = h.groupby("date").agg(precip_delhi_mm=("precip_mm_delhi_mean", "sum"), hours=("precip_mm_delhi_mean", "size")).reset_index()
    d = d[d["hours"] >= 24].drop(columns="hours").sort_values("date").reset_index(drop=True)
    wet = d["precip_delhi_mm"] >= RAIN_DAY_MM
    # only dates actually present; a gap in the record must not silently extend a washout window
    dates = pd.to_datetime(d["date"])
    washout = pd.Series(False, index=d.index)
    for i in np.flatnonzero(wet.to_numpy()):
        washout |= (dates >= dates[i]) & (dates <= dates[i] + pd.Timedelta(days=WASHOUT_DAYS))
    d["washout"] = washout
    d["precip_delhi_mm"] = d["precip_delhi_mm"].round(3)
    return d


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


MET_CONTROLS = ["blh_delhi_max", "t2m_delhi"]


def dry_days_association(daily: pd.DataFrame, exposure_col: str) -> dict | None:
    """The association restricted to days NOT affected by recent rain (washout == False). If rain were
    what made the early days clean, restricting to dry days should weaken the association; if it holds,
    rain is not the explanation. Returns None when there are too few dry days to say anything."""
    if "washout" not in daily.columns:
        return None
    d = daily.dropna(subset=[exposure_col, "pm25_delhi", "washout"])
    dry = d[~d["washout"].astype(bool)]
    if len(dry) < MIN_DAYS_FOR_ASSOCIATION:
        return {"n_dry_days": len(dry), "n_wet_days": int(len(d) - len(dry)), "spearman": None, "perm_p": None, "note": "too few dry days"}
    rho, p = permutation_spearman(dry[exposure_col].to_numpy(), dry["pm25_delhi"].to_numpy())
    return {"n_dry_days": len(dry), "n_wet_days": int(len(d) - len(dry)), "spearman": round(rho, 3), "perm_p": round(p, 4), "note": ""}


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
            dd = d.dropna(subset=["wind_delhi"]) if "wind_delhi" in d else d.iloc[0:0]
            if len(dd) >= MIN_DAYS_FOR_ASSOCIATION:
                prho, pp = partial_spearman(dd["x"].to_numpy(), dd["pm25_delhi"].to_numpy(), dd["wind_delhi"].to_numpy())
            # The control the first pass lacked: boundary-layer height + temperature (season onset).
            mrho = mp = mcp = np.nan
            if all(c in d.columns for c in MET_CONTROLS):
                dm = d.dropna(subset=MET_CONTROLS)
                if len(dm) >= MIN_DAYS_FOR_ASSOCIATION + len(MET_CONTROLS):
                    z = dm[MET_CONTROLS].to_numpy(float)
                    mrho, mp = partial_spearman(dm["x"].to_numpy(), dm["pm25_delhi"].to_numpy(), z)
                    _, mcp = partial_circular_shift_p(dm["x"].to_numpy(), dm["pm25_delhi"].to_numpy(), z)
            _, cp = circular_shift_p(d["x"].to_numpy(), d["pm25_delhi"].to_numpy())
            # Does the AMOUNT of exposure matter, or only whether any arrived? Restrict to
            # days with exposure > 0 (removes the clean-vs-dirty step).
            ex = d[d["x"] > 0]
            erho = stats.spearmanr(ex["x"], ex["pm25_delhi"]).statistic if len(ex) >= 6 else np.nan
            rnd = lambda v, k: None if v is None or np.isnan(v) else round(float(v), k)  # noqa: E731
            rows.append({"exposure": col, "lag_days": lag, "n_days": len(d), "spearman": round(rho, 3), "perm_p": round(p, 4),
                         "circ_shift_p": round(cp, 3), "n_exposed_days": len(ex),
                         "spearman_exposed_days_only": rnd(erho, 3),
                         "partial_ctrl_blh_temp": rnd(mrho, 3), "partial_blh_perm_p": rnd(mp, 4), "partial_blh_circ_p": rnd(mcp, 3),
                         "partial_ctrl_station_wind": rnd(prho, 3), "partial_wind_perm_p": rnd(pp, 4)})
    return pd.DataFrame(rows)


def main() -> int:
    fires, wind_raw, openaq = load_fires(), _read_many("era5_wind_*.csv"), _read_many("openaq_hourly_*.csv")
    if fires.empty or wind_raw.empty or openaq.empty:
        print("Need data/raw firms_acq_*, era5_wind_* and openaq_hourly_* files.")
        return 1
    openaq, stuck = drop_flatlined_hours(openaq)
    if len(stuck):
        print(f"QC: removed {int(stuck['hours_removed'].sum())} stuck-sensor station-hours "
              f"({stuck['station'].nunique()} stations) before analysis")
    wind10 = build_wind_field(wind_raw)
    has925 = {"u925_ms", "v925_ms"} <= set(wind_raw.columns) and wind_raw["u925_ms"].notna().any()
    wind925 = build_wind_field(wind_raw.dropna(subset=["u925_ms", "v925_ms"]), "u925_ms", "v925_ms") if has925 else None
    station_city = {}
    for f in sorted(glob.glob(str(RAW_DIR / "cpcb_ncr_*.csv"))):
        d = pd.read_csv(f, usecols=["station", "city"])
        station_city.update(dict(zip(d["station"], d["city"])))

    wind_end = pd.Timestamp(wind10.times[-1])
    print(f"Fires: {len(fires)} detections {fires['acq_date'].min()}..{fires['acq_date'].max()} | "
          f"wind {pd.Timestamp(wind10.times[0]):%Y-%m-%d %H:%M}..{wind_end:%Y-%m-%d %H:%M} UTC | 925 hPa winds: {'yes' if has925 else 'NO (old files)'}")

    daily = baseline_fire_counts(fires).merge(delhi_daily_pm_and_wind(openaq, station_city), on="date", how="outer")
    daily = daily.merge(delhi_daily_met(wind_raw), on="date", how="left")
    precip = _read_many("era5_precip_*.csv")
    daily = daily.merge(delhi_daily_precip(precip), on="date", how="left")

    # PRE-REGISTERED, before looking at any result with these variables: the primary exposure uses 925 hPa
    # winds with no tuning factor, and the primary confound control is boundary-layer height + temperature.
    # The 10 m runs (x1.0/1.5/2.0) are kept only as a sensitivity check against the first pass.
    runs = []
    if has925:
        runs.append(("925", wind925, 1.0))
    runs += [(f"k{k}", wind10, k) for k in SPEED_FACTORS]
    exposure_cols, traj_summary = [], []
    for label, field, k in runs:
        traj = advect_fires(fires, field, speed_factor=k)
        e = daily_exposure(traj).rename(columns={"hit_fires": f"hit_fires_{label}", "hit_frp": f"hit_frp_{label}"})
        daily = daily.merge(e, on="date", how="left")
        for c in (f"hit_fires_{label}", f"hit_frp_{label}"):
            daily[c] = daily[c].fillna(0.0)
        exposure_cols.append(f"hit_frp_{label}")
        h = traj[traj["hit"] & ~traj["local"]]
        traj_summary.append((label, int(traj["local"].sum()), len(h), float(h["travel_h"].median()) if len(h) else np.nan))
    daily = daily.sort_values("date").reset_index(drop=True)

    first_day, last_day = valid_exposure_days(wind10)
    analysis = daily[(daily["date"] >= first_day) & (daily["date"] <= last_day)].copy()
    analysis["fires_near400_f"] = analysis["fires_near400"].astype(float)
    daily["in_analysis_window"] = (daily["date"] >= first_day) & (daily["date"] <= last_day)  # for the dashboard counter
    assoc = association_table(analysis, ["fires_near400_f"] + exposure_cols)

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    daily.to_csv(OUT_DIR / "stubble_transport_daily.csv", index=False)
    assoc.to_csv(OUT_DIR / "stubble_association.csv", index=False)

    print("\nTrajectory summary (run, fires already inside the receptor, transport hits, median travel h):")
    for label, n_local, n_hit, med in traj_summary:
        print(f"  {label:>5}: {n_local} local | {n_hit} transport hits | median {med:.0f} h")
    print(f"\nAnalysis window {first_day}..{last_day} (wind record bounds it: parcels travel up to {MAX_TRAVEL_HOURS} h). "
          f"Days with Delhi PM2.5: {int(analysis['pm25_delhi'].notna().sum())}")
    primary = "hit_frp_925" if has925 else "hit_frp_k1.0"
    show = ["date", "fires_near400", primary, "pm25_delhi", "blh_delhi_max", "t2m_delhi"]
    print(analysis[[c for c in show if c in analysis.columns]].dropna(subset=["pm25_delhi"]).round(1).to_string(index=False))
    print(f"\nAssociation with Delhi daily PM2.5. PRIMARY = {primary}, lag 0, controlling boundary-layer height + temperature:")
    print(assoc.to_string(index=False))
    if "precip_delhi_mm" in analysis.columns and analysis["precip_delhi_mm"].notna().any():
        wet = analysis[analysis["washout"].astype(bool)]
        print(f"\nRain check: {len(wet)} of {int(analysis['precip_delhi_mm'].notna().sum())} analysed days fall in a rain-washout window "
              f"(>= {RAIN_DAY_MM:.0f} mm area-mean rain that day or in the previous {WASHOUT_DAYS} days): "
              f"{', '.join(wet['date']) or 'none'}")
        dry = dry_days_association(analysis, primary)
        print(f"Primary association on rain-free days only: {dry}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
