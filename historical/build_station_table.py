"""Build the all-India station master from the yearly CPCB files, the live feed and OpenAQ.

The yearly CPCB files carry a stable Station ID in every year but no coordinates, so coordinates come
from OpenAQ, whose location names are CPCB's own station names (matched on the exact name). The live
feed (NCR) is then matched to those stations by coordinates, then by name. Output, small and tracked:

    data/processed/historical/station_master.csv   one row per station, with years and record counts

    python historical/build_station_table.py
    python historical/build_station_table.py --refresh-openaq    # re-download the OpenAQ locations
"""
import argparse
import glob
import logging
import os
import re
import sys
import time
from pathlib import Path

import pandas as pd
import requests

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import fetch_openaq as live_oq  # noqa: E402  (API URL and the government-provider list)
import station_master as sm  # noqa: E402

BRONZE = ROOT / "data" / "historical" / "bronze" / "cpcb"
OPENAQ_CACHE = ROOT / "data" / "historical" / "openaq_india_locations.csv"
INDIA_BBOX = "68.0,6.0,98.0,37.5"
PM25 = "PM2.5 (µg/m³)"


def hist_stations(bronze: Path = BRONZE) -> pd.DataFrame:
    """One row per CPCB Station ID: identity plus which years have PM2.5 and how many records."""
    parts = []
    for f in sorted(glob.glob(str(bronze / "cpcb-air-quality-*.parquet"))):
        year = int(re.search(r"(\d{4})\.parquet$", f).group(1))
        d = pd.read_parquet(f, columns=["Station ID", "State", "City", "Station Name", PM25])
        meta = d.drop_duplicates("Station ID")[["Station ID", "State", "City", "Station Name"]]
        n = d.dropna(subset=[PM25]).groupby("Station ID").size().rename("n").reset_index()
        meta = meta.merge(n, on="Station ID", how="left").fillna({"n": 0})
        meta["year"] = year
        parts.append(meta)
    if not parts:
        raise FileNotFoundError(f"no yearly files in {bronze}")
    allp = pd.concat(parts, ignore_index=True)
    withpm = allp[allp["n"] > 0]
    last = allp.sort_values("year").drop_duplicates("Station ID", keep="last")
    out = pd.DataFrame({"source_id": last["Station ID"], "name": last["Station Name"], "city": last["City"], "state": last["State"]})
    g = withpm.groupby("Station ID")
    stats = pd.DataFrame({"first_year": g["year"].min(), "last_year": g["year"].max(), "years_with_pm25": g["year"].nunique(),
                          "pm25_records": g["n"].sum().astype(int)})
    out = out.merge(stats, left_on="source_id", right_index=True, how="left")
    out[["first_year", "last_year"]] = out[["first_year", "last_year"]].astype("Int64")
    out["years_with_pm25"] = out["years_with_pm25"].fillna(0).astype(int)
    out["pm25_records"] = out["pm25_records"].fillna(0).astype(int)
    out["latitude"] = float("nan")
    out["longitude"] = float("nan")
    return out.reset_index(drop=True)


def fetch_india_locations(api_key: str) -> pd.DataFrame:
    rows, page = [], 1
    while True:
        resp = requests.get(f"{live_oq.API_BASE}/locations", params={"bbox": INDIA_BBOX, "limit": 1000, "page": page},
                            headers={"X-API-Key": api_key}, timeout=60)
        resp.raise_for_status()
        payload = resp.json()
        results = payload.get("results", [])
        for x in results:
            rows.append({"id": x["id"], "name": x["name"], "provider": (x.get("provider") or {}).get("name"),
                         "lat": x["coordinates"]["latitude"], "lon": x["coordinates"]["longitude"]})
        print(f"  OpenAQ page {page}: {len(rows)} locations so far", flush=True)
        if len(results) < 1000:  # "found" comes back as ">1000" for big areas, so stop on a short page instead
            break
        page += 1
        time.sleep(1)  # the free key is rate limited
    return pd.DataFrame(rows)


def attach_coordinates(stations: pd.DataFrame, locs: pd.DataFrame) -> pd.DataFrame:
    """Give each CPCB station the coordinates and OpenAQ ID of the OpenAQ location with the same name."""
    gov = locs[locs["provider"].isin(live_oq.GOVERNMENT_PROVIDERS)].copy()
    gov["key"] = gov["name"].str.strip().str.lower()
    gov = gov.drop_duplicates("key")
    out = stations.copy()
    out["key"] = out["name"].str.strip().str.lower()
    out = out.drop(columns=["latitude", "longitude"]).merge(
        gov[["key", "lat", "lon", "id"]].rename(columns={"lat": "latitude", "lon": "longitude", "id": "openaq_id"}), on="key", how="left")
    out["openaq_id"] = out["openaq_id"].astype("Int64")
    return out.drop(columns=["key"])


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--refresh-openaq", action="store_true")
    args = ap.parse_args()
    from dotenv import load_dotenv

    load_dotenv(ROOT / ".env")
    if args.refresh_openaq or not OPENAQ_CACHE.exists():
        key = os.environ.get("OPENAQ_API_KEY")
        if not key:
            print("OPENAQ_API_KEY not set in .env", file=sys.stderr)
            return 1
        print("fetching OpenAQ locations for India")
        OPENAQ_CACHE.parent.mkdir(parents=True, exist_ok=True)
        fetch_india_locations(key).to_csv(OPENAQ_CACHE, index=False)
    locs = pd.read_csv(OPENAQ_CACHE)
    hist = attach_coordinates(hist_stations(), locs)
    print(f"{len(hist)} CPCB stations; with coordinates: {hist['latitude'].notna().sum()}")
    openaq_by_cpcb = hist.set_index("source_id")["openaq_id"]
    master = sm.build_master({"cpcb": hist[sm.SOURCE_COLS], "feed": sm.feed_stations()})
    extra = hist.set_index("source_id")[["first_year", "last_year", "years_with_pm25", "pm25_records"]]
    master = master.merge(extra, left_on="cpcb_id", right_index=True, how="left")
    master["openaq_id"] = master["cpcb_id"].map(openaq_by_cpcb).astype("Int64")
    master["n_sources"] = master[["cpcb_id", "feed_id", "openaq_id"]].notna().sum(axis=1)
    sm.OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    master.to_csv(sm.OUT_PATH, index=False)
    ncr = master[master["city"].isin(["Delhi", "Gurugram", "Noida", "Ghaziabad", "Faridabad"])]
    print(f"{len(master)} stations -> {sm.OUT_PATH}")
    print(f"  in the live feed and the history: {int((master['cpcb_id'].notna() & master['feed_id'].notna()).sum())}; feed only: "
          f"{int((master['cpcb_id'].isna() & master['feed_id'].notna()).sum())}; history only: {int(master['feed_id'].isna().sum())}")
    print(f"  NCR stations in the master: {len(ncr)}")
    return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING)
    sys.exit(main())
