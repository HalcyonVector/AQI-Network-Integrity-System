"""Stubble replication on the 2015 to 2025 seasons, exactly as fixed in docs/STUBBLE_REPLICATION_PROTOCOL.md.

Every analytical step is the 2026 code (scripts/analyze_stubble_transport.py) applied to each season's own inputs:
Suomi NPP fires advected on 925 hPa ERA5 winds, daily exposure at Delhi, partial Spearman on rain-free days controlling
boundary-layer height and temperature. The decision rule across seasons (Stouffer combination, median sign) is in the
protocol and is not tunable here.

    python historical/stubble_replication.py --structure-only   # windows, counts and coverage; no association computed
    python historical/stubble_replication.py                    # the run (once)

Writes data/processed/historical/stubble_replication.csv and stubble_replication_summary.json.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd
from scipy import stats

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import analyze_stubble_transport as st  # noqa: E402  (the 2026 functions, used unchanged)
from windfield import build_wind_field  # noqa: E402

ERA5 = ROOT / "data" / "historical" / "era5"
FIRMS = ROOT / "data" / "historical" / "firms"
PM_DAILY = ROOT / "data" / "processed" / "historical" / "delhi_pm25_daily.csv"
OUT = ROOT / "data" / "processed" / "historical"
YEARS = list(range(2015, 2026))
MIN_DRY_DAYS = 20  # protocol: a season needs at least this many rain-free days with PM2.5
MIN_STATIONS = 3  # protocol: a day needs at least this many Delhi stations
ALPHA = 0.05


# ---------------------------------------------------------------- pure helpers (tested)


def one_sided_p(p_two_sided: float, rho: float) -> float:
    """Positive-direction one-sided p from a two-sided permutation p (symmetric null): p/2 if rho > 0, else 1 - p/2."""
    return p_two_sided / 2.0 if rho > 0 else 1.0 - p_two_sided / 2.0


def stouffer(p_one_sided) -> tuple[float, float]:
    """Equal-weight Stouffer combination. Returns (z, combined one-sided p)."""
    p = np.clip(np.asarray(list(p_one_sided), float), 1e-12, 1 - 1e-12)
    z = float(stats.norm.isf(p).sum() / np.sqrt(len(p)))
    return z, float(stats.norm.sf(z))


def decide(rows: pd.DataFrame) -> dict:
    """The protocol's decision rule over testable seasons."""
    t = rows[rows["testable"]]
    if t.empty:
        return {"testable_seasons": 0, "holds": False, "reason": "no testable season"}
    z, p = stouffer(t["p_one_sided"])
    med = float(t["rho"].median())
    return {"testable_seasons": int(len(t)), "seasons_positive": int((t["rho"] > 0).sum()), "seasons_p_below_0.05": int((t["p_one_sided"] < ALPHA).sum()),
            "stouffer_z": round(z, 3), "combined_p": round(p, 5), "median_rho": round(med, 3), "holds": bool(p < ALPHA and med > 0)}


# ---------------------------------------------------------------- loading


def _era5_frame(kind: str, rename: dict) -> pd.DataFrame:
    import xarray as xr

    frames = []
    for month in (10, 11):
        ds = xr.open_dataset(ERA5 / f"era5_{kind}_{month}_2015-2025.nc")
        if "expver" in ds.dims:
            ds = ds.mean("expver", skipna=True)
        df = ds.to_dataframe().reset_index().rename(columns={"valid_time": "time", **rename})
        if "pressure_level" in df.columns:
            df = df.drop(columns="pressure_level")
        frames.append(df)
    out = pd.concat(frames, ignore_index=True)
    out["time"] = pd.to_datetime(out["time"])
    return out


def load_winds_and_met() -> tuple[pd.DataFrame, pd.DataFrame]:
    wind = _era5_frame("pressure", {"u": "u925_ms", "v": "v925_ms"})[["time", "latitude", "longitude", "u925_ms", "v925_ms"]]
    met = _era5_frame("single", {"blh": "blh_m", "t2m": "t2m_k"})[["time", "latitude", "longitude", "blh_m", "t2m_k"]]
    return wind, met


def load_hourly_rain() -> pd.DataFrame:
    """Hourly area-mean rain in mm, in the same shape the live pipeline stores (time, precip_mm_delhi_mean)."""
    df = _era5_frame("precip", {})
    df["precip_mm"] = (df["tp"] * 1000.0).clip(lower=0.0)
    out = df.groupby("time").agg(precip_mm_delhi_mean=("precip_mm", "mean"), precip_mm_delhi_max=("precip_mm", "max")).reset_index()
    return out


def load_fires(year: int, sources: tuple[str, ...]) -> pd.DataFrame:
    frames = []
    for src in sources:
        p = FIRMS / f"firms_{src}_{year}.csv"
        if p.exists():
            frames.append(pd.read_csv(p, dtype={"acq_time": str}))
    if not frames:
        return pd.DataFrame()
    f = pd.concat(frames, ignore_index=True)
    f["acq_time"] = f["acq_time"].str.zfill(4)
    f["fire_time"] = pd.to_datetime(f["acq_date"] + " " + f["acq_time"].str[:2] + ":" + f["acq_time"].str[2:], utc=True).dt.tz_localize(None)
    f = f[f["confidence"].astype(str).str.lower() != "l"]  # same low-confidence drop as live
    return f.drop_duplicates(["source", "latitude", "longitude", "acq_date", "acq_time"]).reset_index(drop=True)


def delhi_pm(year: int) -> pd.DataFrame:
    d = pd.read_csv(PM_DAILY)
    d = d[d["date"].str.startswith(str(year)) & (d["stations"] >= MIN_STATIONS)]
    return d.rename(columns={"pm25": "pm25_delhi"})[["date", "pm25_delhi", "stations"]]


# ---------------------------------------------------------------- one season


def season_frame(year: int, wind_all: pd.DataFrame, met_all: pd.DataFrame, rain_all: pd.DataFrame, sources: tuple[str, ...]) -> tuple[pd.DataFrame, tuple[str, str], dict] | None:
    fires = load_fires(year, sources)
    if fires.empty:
        return None
    y = lambda df: df[df["time"].dt.year == year]  # noqa: E731
    wind = build_wind_field(y(wind_all).dropna(subset=["u925_ms", "v925_ms"]), "u925_ms", "v925_ms")
    daily = st.baseline_fire_counts(fires)
    daily = daily.merge(delhi_pm(year), on="date", how="left")
    daily = daily.merge(st.delhi_daily_met(y(met_all)), on="date", how="left")
    daily = daily.merge(st.delhi_daily_precip(y(rain_all)), on="date", how="left")
    traj = st.advect_fires(fires, wind, speed_factor=1.0)
    e = st.daily_exposure(traj).rename(columns={"hit_fires": "hit_fires_925", "hit_frp": "hit_frp_925"})
    daily = daily.merge(e, on="date", how="left")
    for c in ("hit_fires_925", "hit_frp_925"):
        daily[c] = daily[c].fillna(0.0)
    first, last = st.valid_exposure_days(wind)
    daily = daily[(daily["date"] >= first) & (daily["date"] <= last)].sort_values("date").reset_index(drop=True)
    daily["fires_near400_f"] = daily["fires_near400"].astype(float)
    h = traj[traj["hit"] & ~traj["local"]]
    info = {"fires": len(fires), "local": int(traj["local"].sum()), "transport_hits": len(h)}
    return daily, (first, last), info


def partial_stat(d: pd.DataFrame, x: str) -> dict:
    """Partial Spearman of x with Delhi PM2.5 controlling BLH and temperature; two-sided permutation p and circular-shift p."""
    dm = d.dropna(subset=[x, "pm25_delhi", *st.MET_CONTROLS])
    if len(dm) < st.MIN_DAYS_FOR_ASSOCIATION + len(st.MET_CONTROLS):
        return {"n": len(dm), "rho": np.nan, "p_two_sided": np.nan, "circ_p": np.nan}
    z = dm[st.MET_CONTROLS].to_numpy(float)
    rho, p = st.partial_spearman(dm[x].to_numpy(), dm["pm25_delhi"].to_numpy(), z)
    _, cp = st.partial_circular_shift_p(dm[x].to_numpy(), dm["pm25_delhi"].to_numpy(), z)
    return {"n": len(dm), "rho": rho, "p_two_sided": p, "circ_p": cp}


def season_row(year: int, wind_all, met_all, rain_all, structure_only: bool) -> dict:
    res = season_frame(year, wind_all, met_all, rain_all, ("VIIRS_SNPP_SP",))
    if res is None:
        return {"year": year, "testable": False, "note": "no fire file"}
    daily, (first, last), info = res
    dry = daily[(daily["washout"] == False)]  # noqa: E712  (NaN rain record is not rain-free)
    dry_pm = dry.dropna(subset=["pm25_delhi", *st.MET_CONTROLS])
    row = {"year": year, "window_first": first, "window_last": last, **info,
           "days_in_window": len(daily), "days_with_pm": int(daily["pm25_delhi"].notna().sum()),
           "median_delhi_stations": float(delhi_pm(year)["stations"].median()) if len(delhi_pm(year)) else np.nan,
           "dry_days_with_pm": len(dry_pm), "testable": len(dry_pm) >= MIN_DRY_DAYS}
    if structure_only:
        return row
    main = partial_stat(dry, "hit_frp_925")
    row.update({"rho": main["rho"], "p_two_sided": main["p_two_sided"], "circ_p": main["circ_p"]})
    row["p_one_sided"] = one_sided_p(main["p_two_sided"], main["rho"]) if np.isfinite(main["rho"]) else np.nan
    allday = partial_stat(daily, "hit_frp_925")
    base = partial_stat(dry, "fires_near400_f")
    row.update({"all_days_n": allday["n"], "all_days_rho": allday["rho"], "all_days_p": allday["p_two_sided"],
                "baseline400_rho": base["rho"], "baseline400_p": base["p_two_sided"]})
    if year >= 2018:  # secondary 4: Suomi NPP and NOAA-20 together
        two = season_frame(year, wind_all, met_all, rain_all, ("VIIRS_SNPP_SP", "VIIRS_NOAA20_SP"))
        if two is not None:
            d2 = two[0]
            s2 = partial_stat(d2[d2["washout"] == False], "hit_frp_925")  # noqa: E712
            row.update({"two_sensor_rho": s2["rho"], "two_sensor_p": s2["p_two_sided"]})
    row["testable"] = bool(row["testable"] and np.isfinite(row["rho"]))
    return row


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--structure-only", action="store_true")
    args = ap.parse_args()
    wind, met = load_winds_and_met()
    rain = load_hourly_rain()
    rows = []
    for yr in YEARS:
        r = season_row(yr, wind, met, rain, args.structure_only)
        rows.append(r)
        print({k: (round(v, 3) if isinstance(v, float) else v) for k, v in r.items()}, flush=True)
    df = pd.DataFrame(rows)
    OUT.mkdir(parents=True, exist_ok=True)
    if args.structure_only:
        print("structure only: no association computed, nothing written")
        return 0
    df.to_csv(OUT / "stubble_replication.csv", index=False)
    summary = decide(df)
    (OUT / "stubble_replication_summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
