"""
Pull CPCB station readings for the NCR from OpenAQ's open archive, as a second
source alongside the data.gov.in feed (which has been unreachable since ~26 Sep 2026
and, per a cross-check, appears to carry AQI sub-index values rather than
concentrations -- see README). OpenAQ's archive has true concentrations at ~15-minute
resolution, plus station-level met variables (wind, humidity).

Usage:
    python scripts/fetch_openaq.py                      # last 10 days (idempotent)
    python scripts/fetch_openaq.py --start 2026-09-26 --end 2026-10-07   # backfill
    python scripts/fetch_openaq.py --refresh-map        # rebuild station mapping

Station discovery needs a free OPENAQ_API_KEY (explore.openaq.org). The daily files
themselves are read from OpenAQ's public S3 archive with no credentials.

Output: data/raw/openaq_hourly_YYYY-MM-DD.csv, ONE FILE PER OBSERVATION DATE (not per
fetch date like the other pulls), overwritten on re-run so backfills and overlapping
daily runs are idempotent. Long format with a units column, because units differ by
station and parameter (e.g. one station reports NO2 in ppb and O3 in ug/m3).
"""

from __future__ import annotations

import argparse
import gzip
import io
import logging
import math
import os
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd
import requests
from dotenv import load_dotenv

API_BASE = "https://api.openaq.org/v3"
S3_BASE = "https://openaq-data-archive.s3.amazonaws.com/records/csv.gz"

# west, south, east, north -- covers Delhi, Noida, Gurugram, Ghaziabad, Faridabad.
NCR_BBOX = "76.7,28.2,77.7,28.95"
ACTIVE_WITHIN_DAYS = 90  # OpenAQ keeps stale duplicate locations (some last reported in 2018)
GOVERNMENT_PROVIDERS = {"CPCB", "N/A"}
MATCH_MAX_DIST_M = 300
# The archive stalled for 5 days (7-12 Oct 2026) and then back-filled them a week later, so a
# short window misses it. Re-processing is idempotent (one overwritten file per date).
DEFAULT_LOOKBACK_DAYS = 10
IST = "Asia/Kolkata"

KEEP_PARAMETERS = {
    "pm25", "pm10", "no2", "so2", "co", "o3", "no", "nox",
    "relativehumidity", "temperature", "wind_speed", "wind_direction",
}
CIRCULAR_PARAMETERS = {"wind_direction"}

PROJECT_ROOT = Path(__file__).resolve().parent.parent
RAW_DATA_DIR = PROJECT_ROOT / "data" / "raw"
REFERENCE_DIR = PROJECT_ROOT / "data" / "reference"
STATION_MAP_PATH = REFERENCE_DIR / "openaq_station_map.csv"
LOG_DIR = PROJECT_ROOT / "logs"


def setup_logging() -> logging.Logger:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    log_path = LOG_DIR / f"fetch_openaq_{datetime.now():%Y-%m-%d}.log"
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[logging.FileHandler(log_path, encoding="utf-8"), logging.StreamHandler(sys.stdout)],
    )
    return logging.getLogger("fetch_openaq")


# ---------------------------------------------------------------- station mapping


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    rad = math.pi / 180
    a = (
        math.sin((lat2 - lat1) * rad / 2) ** 2
        + math.cos(lat1 * rad) * math.cos(lat2 * rad) * math.sin((lon2 - lon1) * rad / 2) ** 2
    )
    return 2 * 6371000 * math.asin(math.sqrt(a))


def match_stations(ours: pd.DataFrame, openaq: pd.DataFrame) -> pd.DataFrame:
    """Map each of our station names to ONE active OpenAQ location.

    ours:   station, latitude, longitude
    openaq: id, name, lat, lon  (already filtered to active locations)

    Match by coordinates within MATCH_MAX_DIST_M (nearest wins), else by identical
    normalized name. Anything else is left unmapped on purpose: the nearest remaining
    candidate for several IITM/IMD-run stations is a *different* station 2-3 km away,
    and a wrong match silently corrupts every downstream comparison.
    """
    by_name = {n.strip().lower(): r for n, r in zip(openaq["name"], openaq.itertuples())}
    rows = []
    for s in ours.itertuples():
        match, method, dist = None, None, None
        if pd.notna(s.latitude) and pd.notna(s.longitude) and len(openaq):
            dists = openaq.apply(lambda r: haversine_m(s.latitude, s.longitude, r.lat, r.lon), axis=1)
            j = dists.idxmin()
            if dists[j] <= MATCH_MAX_DIST_M:
                match, method, dist = openaq.loc[j], "coordinates", float(dists[j])
        if match is None and s.station.strip().lower() in by_name:
            r = by_name[s.station.strip().lower()]
            match = openaq[openaq["id"] == r.id].iloc[0]
            method = "name"
            dist = haversine_m(s.latitude, s.longitude, match["lat"], match["lon"]) if pd.notna(s.latitude) else None
        if match is not None:
            rows.append(
                {
                    "station": s.station,
                    "openaq_location_id": int(match["id"]),
                    "openaq_name": match["name"],
                    "match_method": method,
                    "distance_m": None if dist is None else round(dist),
                }
            )
    return pd.DataFrame(rows, columns=["station", "openaq_location_id", "openaq_name", "match_method", "distance_m"])


def fetch_openaq_locations(api_key: str, logger: logging.Logger) -> pd.DataFrame:
    rows, page = [], 1
    while True:
        resp = requests.get(
            f"{API_BASE}/locations",
            params={"bbox": NCR_BBOX, "limit": 1000, "page": page},
            headers={"X-API-Key": api_key},
            timeout=60,
        )
        resp.raise_for_status()
        payload = resp.json()
        results = payload.get("results", [])
        for x in results:
            rows.append(
                {
                    "id": x["id"],
                    "name": x["name"],
                    "provider": (x.get("provider") or {}).get("name"),
                    "lat": x["coordinates"]["latitude"],
                    "lon": x["coordinates"]["longitude"],
                    "last": (x.get("datetimeLast") or {}).get("utc"),
                }
            )
        found = int(payload.get("meta", {}).get("found", len(rows)) or len(rows))
        if not results or len(rows) >= found:
            break
        page += 1
    df = pd.DataFrame(rows)
    logger.info("OpenAQ returned %d locations in the NCR bounding box", len(df))
    return df


def select_active_locations(locs: pd.DataFrame, now: pd.Timestamp) -> pd.DataFrame:
    """Keep recently-reporting locations from the government networks only.

    OpenAQ tags CPCB-network stations with provider "CPCB" *or* "N/A" -- most DPCC and
    UPPCB stations (JNU, Govindpuram, Wave City, ...) are filed under "N/A", so a
    "CPCB"-only filter silently dropped 10 real stations. Low-cost sensor networks
    (AirGradient, Clarity, AirNow) are excluded: one sitting within 300 m of a
    reference station would otherwise be matched in its place.
    """
    last = pd.to_datetime(locs["last"], utc=True, errors="coerce")
    recent = last >= now - pd.Timedelta(days=ACTIVE_WITHIN_DAYS)
    return locs[recent & locs["provider"].isin(GOVERNMENT_PROVIDERS)].reset_index(drop=True)


def build_station_map(api_key: str, logger: logging.Logger) -> pd.DataFrame:
    locs = fetch_openaq_locations(api_key, logger)
    active = select_active_locations(locs, pd.Timestamp.now(tz="UTC"))
    logger.info("%d of those are active government-network locations", len(active))

    frames = [pd.read_csv(p, usecols=["station", "latitude", "longitude"]) for p in sorted(RAW_DATA_DIR.glob("cpcb_ncr_*.csv"))]
    if not frames:
        raise RuntimeError("No data/raw/cpcb_ncr_*.csv yet -- need our station list (names + coordinates) to match against.")
    ours = pd.concat(frames).drop_duplicates("station", keep="last")

    mapping = match_stations(ours, active)
    unmatched = sorted(set(ours["station"]) - set(mapping["station"]))
    logger.info("Matched %d/%d of our stations to OpenAQ. Unmatched: %s", len(mapping), len(ours), unmatched)

    REFERENCE_DIR.mkdir(parents=True, exist_ok=True)
    mapping.to_csv(STATION_MAP_PATH, index=False)
    return mapping


# ---------------------------------------------------------------- archive reads


def archive_url(location_id: int, day: datetime) -> str:
    return (
        f"{S3_BASE}/locationid={location_id}/year={day.year}/month={day.month:02d}/"
        f"location-{location_id}-{day:%Y%m%d}.csv.gz"
    )


def fetch_archive_day(location_id: int, day: datetime) -> pd.DataFrame | None:
    resp = requests.get(archive_url(location_id, day), timeout=60)
    if resp.status_code in (403, 404):
        return None  # no file for that station/day (S3 answers 403 or 404 for absent keys)
    resp.raise_for_status()
    raw = gzip.decompress(resp.content)
    # latin-1, not utf-8: the units column holds a raw 0xB5 byte for "micro".
    return pd.read_csv(io.StringIO(raw.decode("latin-1")))


def circular_mean_deg(values: pd.Series) -> float:
    rad = np.radians(values.dropna().to_numpy(dtype=float))
    if rad.size == 0:
        return float("nan")
    return float(np.degrees(np.arctan2(np.sin(rad).mean(), np.cos(rad).mean())) % 360)


def to_hourly_long(records: pd.DataFrame, station: str, day: datetime) -> pd.DataFrame:
    """15-minute archive records -> hourly means (IST, hour start), long format.

    Wind direction uses a circular mean (the arithmetic mean of 350 and 10 degrees is
    180, not 0). Rows outside the requested IST calendar day are dropped, so files
    that straddle midnight don't leak into the neighbouring date's output.
    """
    cols = ["hour_start_ist", "station", "openaq_location_id", "parameter", "units", "value", "n_obs"]
    if records is None or records.empty:
        return pd.DataFrame(columns=cols)

    df = records[records["parameter"].isin(KEEP_PARAMETERS)].copy()
    df["ts"] = pd.to_datetime(df["datetime"], utc=True).dt.tz_convert(IST)
    day_start = pd.Timestamp(day.strftime("%Y-%m-%d"), tz=IST)
    df = df[(df["ts"] >= day_start) & (df["ts"] < day_start + pd.Timedelta(days=1))]
    if df.empty:
        return pd.DataFrame(columns=cols)
    df["hour_start_ist"] = df["ts"].dt.floor("h")

    out = []
    for (hour, param, units), g in df.groupby(["hour_start_ist", "parameter", "units"]):
        value = circular_mean_deg(g["value"]) if param in CIRCULAR_PARAMETERS else g["value"].mean()
        out.append(
            {
                "hour_start_ist": hour.isoformat(),
                "station": station,
                "openaq_location_id": int(g["location_id"].iloc[0]),
                "parameter": param,
                "units": units,
                "value": round(float(value), 4),
                "n_obs": int(g["value"].notna().sum()),
            }
        )
    return pd.DataFrame(out, columns=cols)


def warn_on_unexpected_units(df: pd.DataFrame, logger: logging.Logger) -> None:
    pm = df[df["parameter"].isin(["pm25", "pm10"])]
    bad = pm[~pm["units"].str.contains("g/m", na=False)]
    if not bad.empty:
        logger.warning("PM readings with non-ug/m3 units: %s", sorted(bad["units"].unique()))


# ---------------------------------------------------------------- orchestration


def fetch_date(mapping: pd.DataFrame, day: datetime, logger: logging.Logger) -> pd.DataFrame:
    def one(row):
        try:
            rec = fetch_archive_day(int(row.openaq_location_id), day)
        except requests.RequestException as exc:
            logger.warning("Fetch failed for %s (%s): %s", row.station, row.openaq_location_id, exc)
            return pd.DataFrame()
        return to_hourly_long(rec, row.station, day)

    with ThreadPoolExecutor(max_workers=8) as pool:
        frames = list(pool.map(one, mapping.itertuples()))
    frames = [f for f in frames if not f.empty]
    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()


def write_date_file(df: pd.DataFrame, day: datetime, logger: logging.Logger) -> Path:
    RAW_DATA_DIR.mkdir(parents=True, exist_ok=True)
    out_path = RAW_DATA_DIR / f"openaq_hourly_{day:%Y-%m-%d}.csv"
    df.to_csv(out_path, index=False)  # overwrite: one file per observation date
    logger.info("Wrote %d rows (%d stations) to %s", len(df), df["station"].nunique(), out_path.name)
    return out_path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--start", help="YYYY-MM-DD (default: today-10 UTC)")
    parser.add_argument("--end", help="YYYY-MM-DD inclusive (default: yesterday UTC)")
    parser.add_argument("--refresh-map", action="store_true", help="rebuild the OpenAQ station mapping")
    args = parser.parse_args()

    load_dotenv(PROJECT_ROOT / ".env")
    logger = setup_logging()

    today = datetime.now(timezone.utc).replace(tzinfo=None, hour=0, minute=0, second=0, microsecond=0)
    end = datetime.strptime(args.end, "%Y-%m-%d") if args.end else today - timedelta(days=1)
    start = datetime.strptime(args.start, "%Y-%m-%d") if args.start else today - timedelta(days=DEFAULT_LOOKBACK_DAYS)

    if args.refresh_map or not STATION_MAP_PATH.exists():
        api_key = os.environ.get("OPENAQ_API_KEY")
        if not api_key:
            logger.error("OPENAQ_API_KEY not set (needed once to build the station map). Add it to .env.")
            return 1
        try:
            mapping = build_station_map(api_key, logger)
        except Exception:
            logger.exception("Failed to build the OpenAQ station map")
            return 1
    else:
        mapping = pd.read_csv(STATION_MAP_PATH)

    day = start
    while day <= end:
        df = fetch_date(mapping, day, logger)
        if df.empty:
            logger.warning("No OpenAQ data for %s (archive may not have published it yet).", day.strftime("%Y-%m-%d"))
        else:
            warn_on_unexpected_units(df, logger)
            write_date_file(df, day, logger)
        day += timedelta(days=1)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
