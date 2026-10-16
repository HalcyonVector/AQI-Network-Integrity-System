"""
Component 3 (and the backtest for component 2): station health from neighbour consistency.

Needs no satellite, so it works at night and under cloud, when AOD is blind.

Idea: a station's hourly PM2.5 should be predictable from its nearby neighbours' PM2.5 at
the same hour, up to a persistent site offset. Each station is predicted LEAVE-ONE-OUT
(its own reading is never an input) in log space, with the site offset estimated as the
median residual.

Three questions, answered with data rather than assumed:
  1. Does conditioning on wind help? Compare predicting from ALL near neighbours against
     predicting from only UPWIND neighbours (bearing within +/-60 deg of the wind-from
     direction, ERA5 wind >= 1.5 m/s). It is a plausible physical idea, not a given.
  2. Which stations misbehave? Flatlines (stuck sensor), sustained departures from the
     neighbour prediction, and slow drift of the residual (Kendall trend, BH-FDR).
  3. How good is gap-filling from neighbours? A masked-block backtest against persistence
     and same-hour-yesterday baselines.

Usage:
    python scripts/analyze_neighbor_consistency.py
Writes data/processed/neighbor_station_health.csv, neighbor_predictor_comparison.csv,
gapfill_backtest.csv.
"""

from __future__ import annotations

import glob
import sys
import warnings
from pathlib import Path

import numpy as np
import pandas as pd
from scipy import stats
from windfield import build_wind_field, haversine_km, wind_at

PROJECT_ROOT = Path(__file__).resolve().parent.parent
RAW_DIR = PROJECT_ROOT / "data" / "raw"
OUT_DIR = PROJECT_ROOT / "data" / "processed"

NEIGHBOR_RADIUS_KM = 25.0
K_NEIGHBORS = 6
MIN_NEIGHBORS = 3
UPWIND_HALF_ANGLE_DEG = 60.0
UPWIND_MIN_SPEED_MS = 1.5
MIN_UPWIND = 2
PM_MAX = 1500.0  # above this a reading is treated as a glitch
IST_OFFSET = pd.Timedelta(hours=5, minutes=30)

FLATLINE_MIN_HOURS = 6
ANOMALY_Z = 3.0
ANOMALY_ROLL_HOURS = 6
ANOMALY_MIN_HOURS = 12
MIN_HOURS_PER_STATION = 72
MIN_DAYS_FOR_TREND = 10
FDR = 0.10
# Significance is not importance: with enough hours even a 0.3%/day creep is 'significant'. Only
# drift of at least ~2%/day (about a third over two weeks) is worth a human's attention.
MIN_TREND_LOG_PER_DAY = 0.02
# Same principle for 'noisy': in a uniform network the MAD of sigmas is tiny, so the statistical cut
# alone flags ordinary sampling variation. Also require at least 1.5x the typical residual noise.
NOISY_MIN_RATIO = 1.5
# The trend test stays over-confident even after the autocorrelation correction (measured: ~35% false
# positives on random-walk-like series at n=17, vs 5% nominal). So drift alone is a FLAG only when the
# total change over the period is large -- a JUDGEMENT call, not tuned to the data: 0.5 log units is a
# 65% change in the station's reading relative to its neighbours. Smaller significant drifts are "watch".
MIN_TOTAL_DRIFT_LOG = 0.5
MIN_WATCH_DRIFT_LOG = float(np.log(2))  # a doubling/halving vs neighbours earns a human glance


# ---------------------------------------------------------------- data


def pm_matrix(openaq_long: pd.DataFrame) -> pd.DataFrame:
    """Hourly PM2.5 as a (hours x stations) frame, IST hour start, on a complete hourly grid.
    Non-positive or implausibly large readings become NaN."""
    pm = openaq_long[openaq_long["parameter"] == "pm25"].copy()
    pm["ts"] = pd.to_datetime(pm["hour_start_ist"].str[:19])
    m = pm.pivot_table(index="ts", columns="station", values="value", aggfunc="mean")
    m = m.reindex(pd.date_range(m.index.min(), m.index.max(), freq="h"))
    return m.where((m > 0) & (m <= PM_MAX))


def bearing_deg(lat1, lon1, lat2, lon2):
    """Initial great-circle bearing from point 1 to point 2, degrees clockwise from north."""
    r = np.pi / 180
    dlon = (lon2 - lon1) * r
    y = np.sin(dlon) * np.cos(lat2 * r)
    x = np.cos(lat1 * r) * np.sin(lat2 * r) - np.sin(lat1 * r) * np.cos(lat2 * r) * np.cos(dlon)
    return (np.degrees(np.arctan2(y, x)) + 360) % 360


def build_neighbors(coords: pd.DataFrame, stations: list[str], radius_km=NEIGHBOR_RADIUS_KM, k=K_NEIGHBORS) -> dict[int, dict]:
    """For each station index: its up-to-k nearest neighbours within radius_km, with distance
    and bearing (station -> neighbour). Stations without coordinates get no neighbours."""
    c = coords.drop_duplicates("station").set_index("station").reindex(stations)
    lat, lon = c["latitude"].to_numpy(float), c["longitude"].to_numpy(float)
    out = {}
    for i in range(len(stations)):
        if not (np.isfinite(lat[i]) and np.isfinite(lon[i])):
            out[i] = {"idx": np.array([], int), "dist": np.array([]), "bearing": np.array([])}
            continue
        d = haversine_km(lat[i], lon[i], lat, lon)
        d[i] = np.inf
        d[~np.isfinite(lat)] = np.inf
        order = np.argsort(d)[:k]
        order = order[d[order] <= radius_km]
        out[i] = {"idx": order, "dist": d[order], "bearing": bearing_deg(lat[i], lon[i], lat[order], lon[order])}
    return out


def station_wind(pm_index: pd.DatetimeIndex, coords: pd.DataFrame, stations: list[str], wind) -> tuple[np.ndarray, np.ndarray]:
    """ERA5 wind speed (m/s) and wind-FROM direction (deg) at every station-hour; NaN where
    the wind record has no coverage."""
    t_utc = (pm_index - IST_OFFSET).to_numpy().astype("datetime64[ns]")
    c = coords.drop_duplicates("station").set_index("station").reindex(stations)
    speed = np.full((len(pm_index), len(stations)), np.nan)
    wfrom = np.full_like(speed, np.nan)
    for j, s in enumerate(stations):
        la, lo = c.loc[s, "latitude"], c.loc[s, "longitude"]
        if not (np.isfinite(la) and np.isfinite(lo)):
            continue
        u, v, ok = wind_at(wind, t_utc, np.full(len(t_utc), la), np.full(len(t_utc), lo))
        speed[ok, j] = np.hypot(u[ok], v[ok])
        wfrom[ok, j] = np.degrees(np.arctan2(-u[ok], -v[ok])) % 360
    return speed, wfrom


# ---------------------------------------------------------------- predictors


def _nanmedian_rows(a: np.ndarray) -> np.ndarray:
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)
        return np.nanmedian(a, axis=1)


def predict_all(logp: np.ndarray, neighbors: dict, speed: np.ndarray, wfrom: np.ndarray) -> dict[str, np.ndarray]:
    """Leave-one-out predictions (log space) for every station-hour, three ways.

    knn_median:    median of the (up to K) nearest neighbours' log PM2.5.
    idw_mean:      inverse-distance-weighted mean of the same neighbours.
    upwind_median: median over the UPWIND subset when wind >= UPWIND_MIN_SPEED_MS and at least
                   MIN_UPWIND upwind neighbours report; otherwise falls back to knn_median.
    Also returns used_upwind (bool matrix): where the upwind rule actually applied.
    """
    T, S = logp.shape
    knn = np.full((T, S), np.nan)
    idw = np.full((T, S), np.nan)
    up = np.full((T, S), np.nan)
    used = np.zeros((T, S), dtype=bool)
    for i in range(S):
        nb = neighbors[i]
        if len(nb["idx"]) == 0:
            continue
        L = logp[:, nb["idx"]]
        valid = np.isfinite(L)
        enough = valid.sum(1) >= MIN_NEIGHBORS
        med = _nanmedian_rows(L)
        knn[enough, i] = med[enough]
        w = 1.0 / nb["dist"][None, :]
        wv = np.where(valid, w, 0.0)
        with np.errstate(invalid="ignore", divide="ignore"):
            idw_i = (np.where(valid, L, 0.0) * wv).sum(1) / wv.sum(1)
        idw[enough, i] = idw_i[enough]

        ang = np.abs(((nb["bearing"][None, :] - wfrom[:, [i]] + 180) % 360) - 180)
        upwind = (ang <= UPWIND_HALF_ANGLE_DEG) & (speed[:, [i]] >= UPWIND_MIN_SPEED_MS)
        sel = valid & upwind
        up_med = _nanmedian_rows(np.where(sel, L, np.nan))
        use = (sel.sum(1) >= MIN_UPWIND) & enough
        up[:, i] = np.where(use, up_med, knn[:, i])
        used[:, i] = use
    return {"knn_median": knn, "idw_mean": idw, "upwind_median": up, "used_upwind": used}


def offset_corrected(logp: np.ndarray, pred: np.ndarray, exclude: np.ndarray | None = None) -> tuple[np.ndarray, np.ndarray]:
    """Return (prediction + per-station median offset, offsets). `exclude` (bool T x S) marks
    hours not allowed to inform the offset (used by the gap-fill backtest)."""
    resid = logp - pred
    if exclude is not None:
        resid = np.where(exclude, np.nan, resid)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)
        off = np.nanmedian(resid, axis=0)
    off = np.where(np.isfinite(off), off, 0.0)
    return pred + off[None, :], off


def compare_predictors(logp: np.ndarray, preds: dict, speed: np.ndarray, stations: list[str]) -> tuple[pd.DataFrame, dict]:
    """MAE (log units) of each predictor overall, in calm vs windy hours, and head-to-head on
    exactly the hours where the upwind rule applied."""
    used = preds["used_upwind"]
    corrected = {k: offset_corrected(logp, preds[k])[0] for k in ("knn_median", "idw_mean", "upwind_median")}
    err = {k: np.abs(logp - v) for k, v in corrected.items()}
    common = np.isfinite(err["knn_median"]) & np.isfinite(err["idw_mean"]) & np.isfinite(err["upwind_median"])
    windy = np.nan_to_num(speed, nan=0.0) >= UPWIND_MIN_SPEED_MS
    regimes = {"all hours": common, "calm (<1.5 m/s)": common & ~windy, "windy (>=1.5 m/s)": common & windy,
               "hours the upwind rule applied": common & used}
    rows = []
    for name, mask in regimes.items():
        for k in err:
            rows.append({"regime": name, "predictor": k, "n_station_hours": int(mask.sum()),
                         "mae_log": round(float(np.nanmean(err[k][mask])), 4) if mask.any() else np.nan,
                         "typical_pct_error": round(float(np.expm1(np.nanmean(err[k][mask])) * 100), 1) if mask.any() else np.nan})
    table = pd.DataFrame(rows)

    # Paired head-to-head, per station, on hours the upwind rule applied.
    m = common & used
    diffs = []
    for j in range(logp.shape[1]):
        mj = m[:, j]
        if mj.sum() >= 12:
            diffs.append(float(np.mean(err["knn_median"][mj, j] - err["upwind_median"][mj, j])))
    paired = {"n_stations": len(diffs), "median_improvement_log": float(np.median(diffs)) if diffs else np.nan,
              "stations_improved": int(np.sum(np.array(diffs) > 0)) if diffs else 0,
              "wilcoxon_p": float(stats.wilcoxon(diffs).pvalue) if len(diffs) >= 8 and np.any(np.array(diffs) != 0) else np.nan}
    return table, paired


# ---------------------------------------------------------------- station health


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
    """Runs where the hourly value repeats EXACTLY (a stuck sensor), as (start, n_hours)
    counting the first hour of the run, with at least min_hours identical consecutive hours."""
    same = np.zeros(len(values), dtype=bool)
    same[1:] = np.isfinite(values[1:]) & np.isfinite(values[:-1]) & (np.abs(values[1:] - values[:-1]) < 1e-9)
    runs = []
    for start, length in run_lengths(same):
        if length + 1 >= min_hours:
            runs.append((start - 1, length + 1))
    return runs


def benjamini_hochberg(p: np.ndarray) -> np.ndarray:
    p = np.asarray(p, float)
    m = len(p)
    order = np.argsort(p)
    ranked = p[order] * m / np.arange(1, m + 1)
    q = np.minimum.accumulate(ranked[::-1])[::-1].clip(max=1.0)
    out = np.empty(m)
    out[order] = q
    return out


def trend_test(y: np.ndarray) -> dict:
    """Theil-Sen slope with a Mann-Kendall test corrected for serial correlation.

    Daily residuals are autocorrelated (a station that is high today is likely high
    tomorrow), which makes the plain Kendall test badly over-confident: on the first real
    run it flagged 14 of 61 stations. Correction: shrink the Mann-Kendall z-score by
    sqrt(n_eff / n), n_eff = n (1 - r1) / (1 + r1), r1 = lag-1 autocorrelation of the
    detrended series (only positive r1 inflates, so it is clipped at 0).
    """
    n = len(y)
    x = np.arange(n)
    slope, intercept, _, _ = stats.theilslopes(y, x)
    tau = stats.kendalltau(x, y).statistic
    resid = np.asarray(y) - (intercept + slope * x)
    r1 = float(np.corrcoef(resid[:-1], resid[1:])[0, 1]) if n > 3 and np.std(resid) > 0 else 0.0
    r1 = float(np.clip(np.nan_to_num(r1), 0.0, 0.9))
    n_eff = n * (1 - r1) / (1 + r1)
    z = tau / np.sqrt(2 * (2 * n + 5) / (9 * n * (n - 1)))
    z_adj = z * np.sqrt(n_eff / n)
    return {"slope": float(slope), "p_raw": float(stats.kendalltau(x, y).pvalue), "p_norm": float(2 * stats.norm.sf(abs(z))),
            "p_adj": float(2 * stats.norm.sf(abs(z_adj))), "r1": r1}


def classify_station(r: pd.Series, noisy_cut: float) -> tuple[str, str]:
    """Turn one station's statistics into (reason text, level) where level is
    flag | watch | ok | none.

    FLAG   flatline; sustained departure from neighbours; unusual noise; or drift that is
           significant after multiple-testing correction AND large (>= MIN_TOTAL_DRIFT_LOG).
    WATCH  drift that is significant but small; or a LARGE apparent drift (a doubling or
           halving relative to neighbours, ln 2) that is individually significant after the
           autocorrelation correction (p < 0.05) but not after correcting across ~60 stations.
           Cheap for a human to glance at, and explicitly not confirmed.
    """
    if pd.isna(r.get("resid_sigma_log")):
        return r["reason"], "none"
    hard, soft = [], []
    if r["flatline_episodes"] > 0:
        hard.append(f"flatline x{int(r['flatline_episodes'])} ({int(r['flatline_hours'])} h)")
    if r["anomaly_episodes"] > 0:
        hard.append(f"sustained departure from neighbours x{int(r['anomaly_episodes'])} ({int(r['anomaly_hours'])} h)")
    if pd.notna(r["trend_log_per_day"]) and pd.notna(r["trend_q"]):
        total = r["trend_log_per_day"] * (r["trend_days"] - 1)
        label = f"{r['trend_log_per_day']:+.3f} log/day = {np.expm1(total) * 100:+.0f}% over {int(r['trend_days'])} days"
        if r["trend_q"] < FDR and abs(r["trend_log_per_day"]) >= MIN_TREND_LOG_PER_DAY:
            (hard if abs(total) >= MIN_TOTAL_DRIFT_LOG else soft).append(f"drifting vs neighbours ({label}, q={r['trend_q']:.2f})")
        elif r["trend_p"] < 0.05 and abs(total) >= MIN_WATCH_DRIFT_LOG:
            soft.append(f"large apparent drift vs neighbours ({label}; p={r['trend_p']:.3f}, q={r['trend_q']:.2f}: "
                        "not confirmed after multiple-testing correction)")
    if r["resid_sigma_log"] > noisy_cut:
        hard.append("unusually noisy vs neighbours")
    if hard:
        return "; ".join(hard + [f"watch: {x}" for x in soft]), "flag"
    if soft:
        return "watch: " + "; ".join(soft), "watch"
    return "consistent with neighbours", "ok"


def station_health(pm: pd.DataFrame, pred_knn: np.ndarray) -> pd.DataFrame:
    logp = np.log(pm.to_numpy(float))
    corrected, offsets = offset_corrected(logp, pred_knn)
    resid = logp - corrected
    rows = []
    for j, station in enumerate(pm.columns):
        r = resid[:, j]
        n = int(np.isfinite(r).sum())
        if n < MIN_HOURS_PER_STATION:
            rows.append({"station": station, "n_hours": n, "reason": f"insufficient hours ({n} < {MIN_HOURS_PER_STATION})"})
            continue
        # Noise and episodes are measured around the station's OWN slow trend (robust Theil-Sen), so a
        # smooth drift is not double-counted as "noisy" or as a sustained departure -- drift has its
        # own test below. A 40 h episode barely moves a robust slope fitted to 400 h.
        finite = np.isfinite(r)
        hours = np.arange(len(r))
        sl, ic, _, _ = stats.theilslopes(r[finite], hours[finite])
        r_dt = r - (ic + sl * hours)
        sigma = 1.4826 * np.nanmedian(np.abs(r_dt - np.nanmedian(r_dt)))
        z = r_dt / sigma if sigma > 0 else np.full_like(r, np.nan)
        rolled = pd.Series(z).rolling(ANOMALY_ROLL_HOURS, min_periods=ANOMALY_ROLL_HOURS - 2).median().to_numpy()
        episodes = [(s, ln) for s, ln in run_lengths(np.nan_to_num(np.abs(rolled), nan=0.0) >= ANOMALY_Z) if ln >= ANOMALY_MIN_HOURS]
        flats = flatline_runs(pm.iloc[:, j].to_numpy(float))
        daily = pd.Series(r, index=pm.index).groupby(pm.index.date).agg(["median", "count"])
        daily = daily[daily["count"] >= 12]["median"]
        slope = p = p_raw = np.nan
        n_trend_days = len(daily)
        if len(daily) >= MIN_DAYS_FOR_TREND:
            tt = trend_test(daily.to_numpy())
            slope, p, p_raw = tt["slope"], tt["p_adj"], tt["p_raw"]
        rows.append({"station": station, "n_hours": n, "site_offset_log": round(float(offsets[j]), 3), "resid_sigma_log": round(float(sigma), 3),
                     "flatline_episodes": len(flats), "flatline_hours": int(sum(ln for _, ln in flats)),
                     "anomaly_episodes": len(episodes), "anomaly_hours": int(sum(ln for _, ln in episodes)),
                     "trend_log_per_day": None if np.isnan(slope) else round(slope, 4), "trend_days": n_trend_days, "trend_p_raw": p_raw, "trend_p": p})
    df = pd.DataFrame(rows)

    have = df["trend_p"].notna()
    df["trend_q"] = np.nan
    if have.any():
        df.loc[have, "trend_q"] = benjamini_hochberg(df.loc[have, "trend_p"].to_numpy())
    sig = df["resid_sigma_log"].dropna()
    noisy_cut = float(max(sig.median() + 3 * 1.4826 * np.median(np.abs(sig - sig.median())), NOISY_MIN_RATIO * sig.median())) if len(sig) >= 8 else np.inf

    evaluated = df.apply(classify_station, axis=1, noisy_cut=noisy_cut, result_type="expand")
    df["reason"], df["level"] = evaluated[0], evaluated[1]
    df["flag"] = df["level"] == "flag"
    df["watch"] = df["level"] == "watch"
    order = {"flag": 0, "watch": 1, "ok": 2, "none": 3}
    return df.sort_values(["level", "anomaly_hours"], key=lambda c: c.map(order) if c.name == "level" else -c.fillna(0)).reset_index(drop=True)


# ---------------------------------------------------------------- gap-fill backtest


def backtest_gapfill(pm: pd.DataFrame, preds: dict, block_lengths=(3, 6, 12, 24), trials_per_length=300, seed=0) -> pd.DataFrame:
    """Mask a block of hours at one station, fill it three ways, and compare to the truth.

    neighbours:  neighbour prediction + site offset (estimated WITHOUT the masked block)
    persistence: last observed value before the block
    yesterday:   the same hour 24 h earlier
    Scored only where all three can produce a value, so the comparison is like for like.
    """
    values = pm.to_numpy(float)
    logp = np.log(values)
    T, S = values.shape
    pred = preds["knn_median"]
    rng = np.random.default_rng(seed)
    rows = []
    for L in block_lengths:
        err = {"neighbours": [], "persistence": [], "yesterday": []}
        truth = []
        attempts = 0
        while len(truth) < trials_per_length and attempts < trials_per_length * 60:
            attempts += 1
            j, t0 = int(rng.integers(0, S)), int(rng.integers(24, T - L))
            blk = slice(t0, t0 + L)
            if not (np.isfinite(values[blk, j]).all() and np.isfinite(pred[blk, j]).all() and np.isfinite(values[t0 - 1, j])
                    and np.isfinite(values[t0 - 24:t0 + L - 24, j]).all()):
                continue
            mask = np.zeros((T, S), dtype=bool)
            mask[blk, j] = True
            resid = np.where(mask[:, j], np.nan, logp[:, j] - pred[:, j])
            if np.isfinite(resid).sum() < 24:
                continue
            fill = np.exp(pred[blk, j] + np.nanmedian(resid))
            actual = values[blk, j]
            err["neighbours"].append(np.mean(np.abs(fill - actual)))
            err["persistence"].append(np.mean(np.abs(values[t0 - 1, j] - actual)))
            err["yesterday"].append(np.mean(np.abs(values[t0 - 24:t0 + L - 24, j] - actual)))
            truth.append(np.mean(actual))
        for method, e in err.items():
            if e:
                rows.append({"block_hours": L, "method": method, "n_trials": len(e), "mae_ugm3": round(float(np.mean(e)), 2),
                             "median_abs_err_ugm3": round(float(np.median(e)), 2), "mean_truth_ugm3": round(float(np.mean(truth)), 1)})
    return pd.DataFrame(rows)


# ---------------------------------------------------------------- main


def _read_many(pattern: str) -> pd.DataFrame:
    files = sorted(glob.glob(str(RAW_DIR / pattern)))
    return pd.concat((pd.read_csv(f) for f in files), ignore_index=True) if files else pd.DataFrame()


def main() -> int:
    openaq, wind_raw, cpcb = _read_many("openaq_hourly_*.csv"), _read_many("era5_wind_*.csv"), _read_many("cpcb_ncr_*.csv")
    if openaq.empty or wind_raw.empty or cpcb.empty:
        print("Need data/raw openaq_hourly_*, era5_wind_* and cpcb_ncr_* files.")
        return 1
    pm = pm_matrix(openaq)
    stations = list(pm.columns)
    coords = cpcb.dropna(subset=["latitude", "longitude"]).drop_duplicates("station", keep="last")[["station", "latitude", "longitude"]]
    wind = build_wind_field(wind_raw)

    neighbors = build_neighbors(coords, stations)
    n_with = sum(len(v["idx"]) >= MIN_NEIGHBORS for v in neighbors.values())
    speed, wfrom = station_wind(pm.index, coords, stations, wind)
    logp = np.log(pm.to_numpy(float))
    preds = predict_all(logp, neighbors, speed, wfrom)

    print(f"{len(stations)} stations, {len(pm)} hours ({pm.index.min():%Y-%m-%d}..{pm.index.max():%Y-%m-%d}); "
          f"{n_with} have >= {MIN_NEIGHBORS} neighbours within {NEIGHBOR_RADIUS_KM:.0f} km; "
          f"upwind rule applied in {preds['used_upwind'].mean() * 100:.1f}% of station-hours")

    table, paired = compare_predictors(logp, preds, speed, stations)
    print("\n== 1. Does wind-conditioning help? (MAE in log units; lower is better) ==")
    print(table.pivot(index="regime", columns="predictor", values="mae_log").round(3).to_string())
    print(f"Head-to-head on hours the upwind rule applied, per station (n={paired['n_stations']}): "
          f"median improvement over all-neighbours = {paired['median_improvement_log']:+.4f} log; "
          f"{paired['stations_improved']}/{paired['n_stations']} stations improved; Wilcoxon p = {paired['wilcoxon_p']:.3f}")

    health = station_health(pm, preds["knn_median"])
    print(f"\n== 2. Station health (flagged {int(health['flag'].sum())}, watch {int(health['watch'].sum())}, of {len(health)}) ==")
    cols = ["station", "n_hours", "flatline_hours", "anomaly_hours", "trend_log_per_day", "trend_q", "reason"]
    print(health[health["flag"]][cols].head(15).to_string(index=False))
    print(f"\n   ... plus {int(health['watch'].sum())} on watch (drift only, below the magnitude bar):")
    print(health[health["watch"]][["station", "trend_log_per_day", "trend_q"]].to_string(index=False))

    bt = backtest_gapfill(pm, preds)
    print("\n== 3. Gap-fill backtest (MAE in ug/m3; masked blocks, scored like for like) ==")
    print(bt.pivot(index="block_hours", columns="method", values="mae_ugm3").to_string())

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    table.to_csv(OUT_DIR / "neighbor_predictor_comparison.csv", index=False)
    health.to_csv(OUT_DIR / "neighbor_station_health.csv", index=False)
    bt.to_csv(OUT_DIR / "gapfill_backtest.csv", index=False)
    return 0


if __name__ == "__main__":
    sys.exit(main())
