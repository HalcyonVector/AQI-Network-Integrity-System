"""
National satellite check: does each station's PM2.5 follow the MODIS AOD pattern like its regional peers?

Same detector as the NCR check (analyze_station_bias.temporal_decoupling: each station's day-to-day Spearman
correlation between its own AOD pixel and its ground PM2.5 over the satellite overpass window, compared with the
group median), with the two changes a national network needs:

  * Peers are regional. Stations are grouped by average-linkage clustering on great-circle distance (cut at
    GROUP_KM), because the AOD to PM2.5 relationship differs between Delhi and Chennai; only groups with at
    least MIN_GROUP_STATIONS testable stations are tested.
  * The multiple-testing correction (Benjamini-Hochberg) runs across every tested station in the country, not
    within each group, so adding regions does not add false positives.

A group whose median correlation is below MIN_SIGNAL_RHO (the satellite does not track PM2.5 there) flags nothing.
A flag is a lead, not a verdict.

Writes data/processed/national/satellite_station.csv and satellite_groups.csv.

    python scripts/analyze_national_satellite.py
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd
from scipy.cluster.hierarchy import fcluster, linkage
from scipy.spatial.distance import squareform

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import analyze_national_live as an  # noqa: E402
import analyze_station_bias as sb  # noqa: E402

RAW = ROOT / "data" / "raw"
OUT = ROOT / "data" / "processed" / "national"
MASTER = ROOT / "data" / "processed" / "historical" / "station_master.csv"
GROUP_KM = 150.0
MIN_GROUP_STATIONS = 6
MIN_DAYS = 8
FDR = 0.10


def haversine_matrix(lat: np.ndarray, lon: np.ndarray) -> np.ndarray:
    la, lo = np.radians(lat), np.radians(lon)
    dla, dlo = la[:, None] - la[None, :], lo[:, None] - lo[None, :]
    a = np.sin(dla / 2) ** 2 + np.cos(la)[:, None] * np.cos(la)[None, :] * np.sin(dlo / 2) ** 2
    return 6371.0 * 2 * np.arcsin(np.sqrt(a))


def regional_groups(coords: pd.DataFrame, km: float = GROUP_KM) -> pd.Series:
    """Group id per station (index = openaq_id): average-linkage clusters cut at `km` great-circle distance."""
    if len(coords) < 2:
        return pd.Series(1, index=coords.index)
    d = haversine_matrix(coords["latitude"].to_numpy(float), coords["longitude"].to_numpy(float))
    np.fill_diagonal(d, 0.0)
    labels = fcluster(linkage(squareform(d, checks=False), method="average"), t=km, criterion="distance")
    return pd.Series(labels, index=coords.index)


def benjamini_hochberg(p: np.ndarray) -> np.ndarray:
    m = len(p)
    order = np.argsort(p)
    ranked = p[order] * m / np.arange(1, m + 1)
    q = np.empty(m)
    q[order] = np.minimum.accumulate(ranked[::-1])[::-1].clip(max=1.0)
    return q


def ground_daily(pm25: pd.DataFrame) -> pd.DataFrame:
    """Station-day PM2.5 over the satellite overpass window (hours SAT_WINDOW_HOURS, IST), at least MIN_WINDOW_HOURS hours."""
    h = pm25[(pm25.index.hour >= sb.SAT_WINDOW_HOURS[0]) & (pm25.index.hour <= sb.SAT_WINDOW_HOURS[1])]
    day = h.index.normalize()
    mean = h.groupby(day).mean().where(h.groupby(day).count() >= sb.MIN_WINDOW_HOURS)
    long = mean.stack().rename("pm25").reset_index()
    long.columns = ["date", "station", "pm25"]
    long["date"] = long["date"].dt.strftime("%Y-%m-%d")
    long["station"] = long["station"].astype(int).astype(str)
    return long


def load_aod(raw_dir: Path = RAW) -> pd.DataFrame:
    frames = []
    for f in sorted(raw_dir.glob("aod_india_*.parquet")):
        d = pd.read_parquet(f)
        d["date"] = f.stem.rsplit("_", 1)[1]
        frames.append(d)
    if not frames:
        return pd.DataFrame(columns=["station", "date", "aod_055"])
    a = pd.concat(frames, ignore_index=True)
    a["station"] = a["openaq_id"].astype(int).astype(str)
    return a[["station", "date", "aod_055"]]


def run(ground: pd.DataFrame, aod: pd.DataFrame, groups: pd.Series, min_days: int = MIN_DAYS, min_group: int = MIN_GROUP_STATIONS) -> tuple[pd.DataFrame, pd.DataFrame]:
    """(station table, group table). `groups` maps station (str) to a group id."""
    joined = sb.join_satellite(ground, aod.rename(columns={"aod_055": sb.AOD_COLUMN}))
    joined["group"] = joined["station"].map(groups)
    parts, group_rows = [], []
    for gid, jg in joined.dropna(subset=["group"]).groupby("group"):
        res = sb.temporal_decoupling(jg.drop(columns="group"), min_days=min_days, fdr=1.0)
        if len(res) < min_group:
            continue
        res["group"] = int(gid)
        parts.append(res)
    if not parts:
        return pd.DataFrame(), pd.DataFrame()
    st = pd.concat(parts, ignore_index=True)
    st["q_global"] = benjamini_hochberg(st["p_low"].to_numpy(float))
    centre = st.groupby("group")["network_median_rho"].transform("first")
    no_signal = centre < sb.MIN_SIGNAL_RHO
    st["flag"] = (st["q_global"] < FDR) & (st["rho"] < centre) & ~no_signal
    watch = ~st["flag"] & (st["p_low"] < 0.05) & (st["rho"] < centre) & ~no_signal
    st["reason"] = np.where(no_signal, "no flag: this region's stations do not track the satellite",
                            np.where(st["flag"], "PM2.5 decoupled from the satellite vs regional peers",
                                     np.where(watch, "watch: looks decoupled, not significant across the country", "tracks the satellite like its peers")))
    g = st.groupby("group").agg(tested=("station", "count"), median_days=("n_days", "median"), median_rho=("network_median_rho", "first"), flagged=("flag", "sum")).reset_index()
    g["gated"] = g["median_rho"] < sb.MIN_SIGNAL_RHO
    return st, g


def main() -> int:
    mats = an.load_matrices()
    aod = load_aod()
    if not mats or aod.empty:
        print("Need data/raw/openaq_india_pm_*.parquet and aod_india_*.parquet.")
        return 0
    pm25, _ = an.clean(mats["pm25"])
    master = pd.read_csv(MASTER).dropna(subset=["openaq_id", "latitude", "longitude"])
    master = master.assign(station=master["openaq_id"].astype(int).astype(str)).drop_duplicates("station").set_index("station")
    groups = regional_groups(master[["latitude", "longitude"]])
    st, g = run(ground_daily(pm25), aod, groups)
    OUT.mkdir(parents=True, exist_ok=True)
    if st.empty:
        print("Not enough paired days yet.")
        return 0
    label = master.groupby(groups)["city"].agg(lambda c: c.mode().iat[0])
    g["region"] = g["group"].map(label)
    st["openaq_id"] = st["station"].astype(int)
    st["name"] = st["station"].map(master["name"])
    st["city"] = st["station"].map(master["city"])
    st["region"] = st["group"].map(label)
    st.drop(columns="station").to_csv(OUT / "satellite_station.csv", index=False)
    g.sort_values("tested", ascending=False).to_csv(OUT / "satellite_groups.csv", index=False)
    print(f"{len(st)} stations tested in {len(g)} regions ({int(g['gated'].sum())} gated off for weak signal); "
          f"{int(st['flag'].sum())} flagged, {int(st['reason'].str.startswith('watch').sum())} on watch")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
