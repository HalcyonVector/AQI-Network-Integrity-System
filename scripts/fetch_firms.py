"""
Pull active-fire detections over the Punjab/Haryana/western-UP/NCR stubble-burning
belt from NASA FIRMS (Fire Information for Resource Management System), for the
regional-transport side of the spike-attribution component (stubble burning vs.
local sources).

Usage:
    python scripts/fetch_firms.py

Requires FIRMS_MAP_KEY in the environment or a .env file (see .env.example).
Get a free key at https://firms.modaps.eosdis.nasa.gov/api/map_key/ (just an email
address, no approval wait). Rate limit is 5,000 transactions per 10-minute window
per key -- irrelevant at a once/day cadence.
"""

from __future__ import annotations

import logging
import os
import sys
import time
from datetime import datetime, timezone
from io import StringIO
from pathlib import Path

import pandas as pd
import requests
from dotenv import load_dotenv

API_BASE = "https://firms.modaps.eosdis.nasa.gov/api/area/csv"

# west,south,east,north -- covers Punjab, Haryana, Delhi NCR, and western/central
# Uttar Pradesh: the belt responsible for the post-monsoon stubble-burning season
# that drives Delhi's October-November PM2.5 spikes. Deliberately wider than the
# NCR fetch's 5 cities, since attribution needs the *source* region, not just the
# region being monitored for impact.
AREA_COORDINATES = "73.5,27.5,81.0,32.5"

# VIIRS 375m resolution is the standard choice for stubble-burning detection
# (finer than MODIS's 1km, catches smaller agricultural fires). Pulling both
# NOAA-20 and NOAA-21 for full daily coverage (their overpass times differ).
# Deliberately NOT using VIIRS_SNPP_NRT: FIRMS announced on 2026-09-17 that Suomi
# NPP product delivery ceases 2026-11-01 -- squarely inside this year's stubble
# season, so a pipeline depending on it would break right when it matters most.
# https://www.earthdata.nasa.gov/data/alerts-outages/suomi-npp-data-product-delivery-cease-november-1-2026
# URL scheme validated against https://firms.modaps.eosdis.nasa.gov/api/area/ on
# 2026-09-17; re-check that page if this starts erroring, the source list is
# versioned there, not hardcoded from memory.
SOURCES = ["VIIRS_NOAA20_NRT", "VIIRS_NOAA21_NRT"]
DAY_RANGE = 1

MAX_RETRIES = 4
RETRY_BACKOFF_SECS = 5
REQUEST_TIMEOUT_SECS = 30

PROJECT_ROOT = Path(__file__).resolve().parent.parent
RAW_DATA_DIR = PROJECT_ROOT / "data" / "raw"
LOG_DIR = PROJECT_ROOT / "logs"

EXPECTED_COLUMNS = {"latitude", "longitude", "acq_date", "acq_time", "confidence", "frp"}


def setup_logging() -> logging.Logger:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    log_path = LOG_DIR / f"fetch_firms_{datetime.now():%Y-%m-%d}.log"
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[logging.FileHandler(log_path, encoding="utf-8"), logging.StreamHandler(sys.stdout)],
    )
    return logging.getLogger("fetch_firms")


def fetch_one_source(map_key: str, source: str, session: requests.Session, logger: logging.Logger) -> pd.DataFrame:
    url = f"{API_BASE}/{map_key}/{source}/{AREA_COORDINATES}/{DAY_RANGE}"

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = session.get(url, timeout=REQUEST_TIMEOUT_SECS)
            resp.raise_for_status()
            break
        except requests.RequestException as exc:
            logger.warning("Request failed for %s (attempt %d/%d): %s", source, attempt, MAX_RETRIES, exc)
            if attempt == MAX_RETRIES:
                logger.error("Giving up on %s after %d attempts.", source, MAX_RETRIES)
                raise
            time.sleep(RETRY_BACKOFF_SECS * attempt)

    body = resp.text.strip()

    # The API returns HTTP 200 with a plain-text error message (not CSV) for bad
    # keys, exceeded quotas, or malformed requests -- doesn't raise_for_status,
    # so it has to be caught by inspecting the body instead of the status code.
    if not body or "," not in body.splitlines()[0]:
        raise ValueError(f"FIRMS API did not return CSV data for {source}. Response: {body[:300]!r}")

    df = pd.read_csv(StringIO(body))

    missing = EXPECTED_COLUMNS - set(df.columns)
    if missing:
        logger.error("FIRMS response for %s is missing expected columns: %s (got: %s)", source, missing, df.columns.tolist())
        raise ValueError(f"Unexpected FIRMS schema for {source}, missing columns: {missing}")

    logger.info("Fetched %d fire detections from %s", len(df), source)
    if not df.empty:
        df.insert(0, "source", source)
    return df


def fetch_fires(map_key: str, logger: logging.Logger) -> pd.DataFrame:
    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
            )
        }
    )

    frames = [fetch_one_source(map_key, source, session, logger) for source in SOURCES]
    non_empty = [f for f in frames if not f.empty]
    if not non_empty:
        return pd.DataFrame()

    combined = pd.concat(non_empty, ignore_index=True)
    logger.info("Combined %d total fire detections across %d source(s)", len(combined), len(SOURCES))
    return combined


def append_to_csv(df: pd.DataFrame, logger: logging.Logger) -> Path:
    RAW_DATA_DIR.mkdir(parents=True, exist_ok=True)
    out_path = RAW_DATA_DIR / f"firms_fires_{datetime.now():%Y-%m-%d}.csv"

    df = df.copy()
    df.insert(0, "fetched_at_utc", datetime.now(timezone.utc).isoformat(timespec="seconds"))

    write_header = not out_path.exists()
    if not write_header:
        existing_cols = pd.read_csv(out_path, nrows=0).columns.tolist()
        if existing_cols != df.columns.tolist():
            raise ValueError(
                f"Column mismatch appending to {out_path}: existing file has {existing_cols}, "
                f"this run produced {df.columns.tolist()}. Refusing to append."
            )

    df.to_csv(out_path, mode="a", header=write_header, index=False)
    logger.info("Appended %d rows to %s", len(df), out_path)
    return out_path


def main() -> int:
    load_dotenv(PROJECT_ROOT / ".env")
    logger = setup_logging()

    map_key = os.environ.get("FIRMS_MAP_KEY")
    if not map_key:
        logger.error("FIRMS_MAP_KEY not set. Get a free key at https://firms.modaps.eosdis.nasa.gov/api/map_key/ and add it to .env.")
        return 1

    try:
        df = fetch_fires(map_key, logger)
    except (requests.RequestException, ValueError):
        return 1

    if df.empty:
        logger.info("No fire detections in the target area for this pull (normal outside burning season).")
        return 0

    append_to_csv(df, logger)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
