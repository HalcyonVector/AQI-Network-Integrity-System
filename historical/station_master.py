"""One station master table with stable IDs across the sources the historical backfill joins.

Station names and IDs differ between the CPCB feed, OpenAQ and the Princeton 2015-2019 dataset, so
every later join goes through this table. A station is matched across sources by coordinates (nearest
within MATCH_MAX_DIST_M, each master row claimed by at most one record per source), then by identical
normalised name within the same city. Anything else becomes its own row on purpose: a wrong match
silently corrupts every downstream comparison, a missing match only shows as `n_sources == 1`.

    python historical/station_master.py
    python historical/station_master.py --princeton-stations path/to/princeton_stations.csv

The Princeton file must have the columns: source_id, name, city, state, latitude, longitude.
Output: data/processed/historical/station_master.csv (small, tracked).
"""
import argparse
import math
import re
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
RAW_DIR = ROOT / "data" / "raw"
REFERENCE_DIR = ROOT / "data" / "reference"
OUT_PATH = ROOT / "data" / "processed" / "historical" / "station_master.csv"
MATCH_MAX_DIST_M = 300  # same threshold the OpenAQ mapping uses
SOURCE_COLS = ["source_id", "name", "city", "state", "latitude", "longitude"]


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    a = math.sin((p2 - p1) / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(math.radians(lon2 - lon1) / 2) ** 2
    return 2 * 6371000 * math.asin(math.sqrt(a))


def slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", str(text).lower()).strip("-")


def core_name(name: str) -> str:
    """Station name without the agency suffix or the city: 'Bawana, Delhi - DPCC' -> 'bawana'."""
    head = str(name).split(" - ")[0]
    return slug(head.split(",")[0])


def station_id(state: str, city: str, name: str) -> str:
    """Stable ID: state, city and the full name (agency included, since one site can host two agencies)."""
    return f"{slug(state)}_{slug(city)}_{slug(name)}"


def build_master(sources: dict[str, pd.DataFrame]) -> pd.DataFrame:
    """Merge source station lists (first source seeds the table) into one master table."""
    names = list(sources)
    rows: list[dict] = []
    for src in names:
        df = sources[src]
        missing = set(SOURCE_COLS) - set(df.columns)
        if missing:
            raise ValueError(f"source '{src}' lacks columns {sorted(missing)}")
        claimed: set[int] = set()
        # nearest-first so the closest pair wins when two records compete for one master row
        pairs = []
        for i, r in enumerate(df.itertuples(index=False)):
            for j, m in enumerate(rows):
                if pd.notna(r.latitude) and pd.notna(m["latitude"]):
                    d = haversine_m(r.latitude, r.longitude, m["latitude"], m["longitude"])
                    if d <= MATCH_MAX_DIST_M:
                        pairs.append((d, i, j))
        match: dict[int, int] = {}
        for d, i, j in sorted(pairs):
            if i not in match and j not in claimed:
                match[i] = j
                claimed.add(j)
        for i, r in enumerate(df.itertuples(index=False)):
            if i not in match:  # name fallback: same city, same core name, row not yet claimed
                for j, m in enumerate(rows):
                    if j not in claimed and slug(m["city"]) == slug(r.city) and core_name(m["name"]) == core_name(r.name):
                        match[i] = j
                        claimed.add(j)
                        break
            if i in match:
                rows[match[i]][f"{src}_id"] = r.source_id
            else:
                row = {"station_id": station_id(r.state, r.city, r.name), "name": r.name, "city": r.city,
                       "state": r.state, "latitude": r.latitude, "longitude": r.longitude}
                row.update({f"{s}_id": None for s in names})
                row[f"{src}_id"] = r.source_id
                rows.append(row)
    master = pd.DataFrame(rows)
    id_cols = [f"{s}_id" for s in names]
    master["n_sources"] = master[id_cols].notna().sum(axis=1)
    if master["station_id"].duplicated().any():  # two sites with an identical full name: keep both, tell them apart
        dup = master["station_id"].duplicated(keep=False)
        master.loc[dup, "station_id"] = master.loc[dup, "station_id"] + "_" + master.loc[dup].groupby("station_id").cumcount().astype(str)
    return master[["station_id", "name", "city", "state", "latitude", "longitude", *id_cols, "n_sources"]]


def feed_stations(raw_dir: Path = RAW_DIR) -> pd.DataFrame:
    """Stations in the newest CPCB feed snapshot (the feed name is its own ID)."""
    files = sorted(raw_dir.glob("cpcb_ncr_*.csv"))
    if not files:
        raise FileNotFoundError("no cpcb_ncr_*.csv in data/raw")
    df = pd.read_csv(files[-1]).drop_duplicates("station", keep="last")
    return pd.DataFrame({"source_id": df["station"], "name": df["station"], "city": df["city"], "state": df["state"],
                         "latitude": df["latitude"], "longitude": df["longitude"]})


def attach_openaq(master: pd.DataFrame, map_path: Path = REFERENCE_DIR / "openaq_station_map.csv") -> pd.DataFrame:
    """The OpenAQ mapping is keyed by feed station name, so it joins straight onto the feed ID."""
    m = pd.read_csv(map_path)[["station", "openaq_location_id"]].rename(columns={"station": "feed_id", "openaq_location_id": "openaq_id"})
    out = master.merge(m, on="feed_id", how="left")
    out["openaq_id"] = out["openaq_id"].astype("Int64")  # IDs, not measurements: no ".0"
    out["n_sources"] = out[[c for c in out.columns if c.endswith("_id") and c != "station_id"]].notna().sum(axis=1)
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--princeton-stations", help="CSV with source_id, name, city, state, latitude, longitude")
    args = ap.parse_args()
    sources = {"feed": feed_stations()}
    if args.princeton_stations:
        sources["princeton"] = pd.read_csv(args.princeton_stations)
    master = attach_openaq(build_master(sources))
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    master.to_csv(OUT_PATH, index=False)
    print(f"{len(master)} stations -> {OUT_PATH}")
    print(master["n_sources"].value_counts().sort_index().to_string())
    return 0


if __name__ == "__main__":
    sys.exit(main())
