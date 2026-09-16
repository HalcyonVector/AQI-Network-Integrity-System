"""
Sample MODIS MAIAC Aerosol Optical Depth (AOD) at each NCR CPCB station's location,
for the satellite-based independent PM2.5 estimate used by drift detection
(component 1) and gap-filling (component 2).

Usage:
    python scripts/fetch_aod.py [--date YYYY-MM-DD]

Defaults to yesterday (UTC) -- MCD19A2 is a daily granule product and "today's"
granules are typically incomplete/not yet published when this would run.

Requires a Google Earth Engine service account. See README.md "Component: Satellite
AOD" for the Cloud project / service account / key setup (all done through your own
Google account -- not something this script can do for you). Then set in .env:
    GEE_SERVICE_ACCOUNT=your-sa@your-project.iam.gserviceaccount.com
    GEE_SERVICE_ACCOUNT_KEY_PATH=path/to/key.json
    GEE_PROJECT=your-gcp-project-id
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
from dotenv import load_dotenv

# Verified against https://developers.google.com/earth-engine/datasets/catalog/MODIS_061_MCD19A2_GRANULES
# on 2026-09-17 -- re-check that page if this starts erroring (NASA/Google have
# deprecated the prior MODIS/006/MCD19A2_GRANULES collection before; version
# strings in the asset ID are not permanent).
AOD_COLLECTION = "MODIS/061/MCD19A2_GRANULES"
AOD_BANDS = ["Optical_Depth_047", "Optical_Depth_055"]
AOD_SCALE_FACTOR = 0.001  # raw band values -> AOD units
PIXEL_SCALE_METERS = 1000

PROJECT_ROOT = Path(__file__).resolve().parent.parent
RAW_DATA_DIR = PROJECT_ROOT / "data" / "raw"
LOG_DIR = PROJECT_ROOT / "logs"

# NCR bounding box (west, south, east, north) -- same region as the CPCB stations,
# generous enough to cover all 5 cities.
NCR_BOUNDS = (76.7, 27.9, 77.6, 28.9)


def setup_logging() -> logging.Logger:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    log_path = LOG_DIR / f"fetch_aod_{datetime.now():%Y-%m-%d}.log"
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[logging.FileHandler(log_path, encoding="utf-8"), logging.StreamHandler(sys.stdout)],
    )
    return logging.getLogger("fetch_aod")


def init_earth_engine(logger: logging.Logger):
    import ee

    service_account = os.environ.get("GEE_SERVICE_ACCOUNT")
    key_path = os.environ.get("GEE_SERVICE_ACCOUNT_KEY_PATH")
    project = os.environ.get("GEE_PROJECT")

    if not (service_account and key_path and project):
        raise RuntimeError(
            "GEE_SERVICE_ACCOUNT, GEE_SERVICE_ACCOUNT_KEY_PATH, and GEE_PROJECT must all be "
            "set in .env. See README.md 'Component: Satellite AOD' for setup."
        )

    credentials = ee.ServiceAccountCredentials(service_account, key_path)
    ee.Initialize(credentials, project=project)
    logger.info("Earth Engine initialized (project=%s, service account=%s)", project, service_account)
    return ee


def load_station_points(logger: logging.Logger) -> pd.DataFrame:
    """Station identity + coordinates, sourced from the most recent CPCB pull so AOD
    is sampled at exactly the same points the drift/gap-fill models compare against."""
    candidates = sorted(RAW_DATA_DIR.glob("cpcb_ncr_*.csv"))
    if not candidates:
        raise RuntimeError(
            "No data/raw/cpcb_ncr_*.csv found -- run scripts/fetch_cpcb.py at least once first "
            "so there's a station list with coordinates to sample AOD at."
        )
    latest = candidates[-1]
    df = pd.read_csv(latest)
    stations = df[["station", "city", "latitude", "longitude"]].drop_duplicates(subset="station")
    stations = stations.dropna(subset=["latitude", "longitude"])
    logger.info("Loaded %d stations with valid coordinates from %s", len(stations), latest.name)
    return stations


def sample_aod(ee_module, stations: pd.DataFrame, date: datetime, logger: logging.Logger) -> pd.DataFrame:
    ee = ee_module
    date_str = date.strftime("%Y-%m-%d")
    next_day_str = (date + timedelta(days=1)).strftime("%Y-%m-%d")

    region = ee.Geometry.Rectangle(list(NCR_BOUNDS))
    collection = ee.ImageCollection(AOD_COLLECTION).filterDate(date_str, next_day_str).filterBounds(region)

    count = collection.size().getInfo()
    if count == 0:
        logger.warning("No MCD19A2 granules found for %s over the NCR bounding box.", date_str)
        return pd.DataFrame()
    logger.info("Found %d MCD19A2 granule(s) for %s", count, date_str)

    # Multiple granules/orbits can cover the region on a given day -- mosaic takes
    # the first valid pixel per location across them.
    image = collection.mosaic()

    qa = image.select("AOD_QA").toInt()
    cloud_mask = qa.rightShift(0).bitwiseAnd(7).eq(1)  # bits 0-2: 1 = clear
    quality_mask = qa.rightShift(8).bitwiseAnd(15).eq(0)  # bits 8-11: 0 = best quality
    good_pixel = cloud_mask.And(quality_mask)

    aod = image.select(AOD_BANDS).updateMask(good_pixel).multiply(AOD_SCALE_FACTOR)

    features = [
        ee.Feature(ee.Geometry.Point([row.longitude, row.latitude]), {"station": row.station, "city": row.city})
        for row in stations.itertuples()
    ]
    points = ee.FeatureCollection(features)

    sampled = aod.sampleRegions(collection=points, scale=PIXEL_SCALE_METERS, geometries=False)
    result = sampled.getInfo()

    out = parse_sampled_result(result, date_str)
    matched = out["aod_047"].notna().sum() if not out.empty else 0
    logger.info("Sampled AOD at %d stations for %s (%d with a valid/unmasked pixel)", len(out), date_str, matched)
    return out


def parse_sampled_result(result: dict, date_str: str) -> pd.DataFrame:
    """Pure function: Earth Engine's sampleRegions().getInfo() GeoJSON-like dict ->
    a flat DataFrame. Split out from sample_aod() so this (the part with actual
    logic worth testing) doesn't require a live Earth Engine connection to test."""
    rows = []
    for feat in result["features"]:
        props = feat["properties"]
        rows.append(
            {
                "station": props.get("station"),
                "city": props.get("city"),
                "aod_047": props.get("Optical_Depth_047"),
                "aod_055": props.get("Optical_Depth_055"),
            }
        )
    out = pd.DataFrame(rows, columns=["station", "city", "aod_047", "aod_055"])
    out.insert(0, "date", date_str)
    return out


def append_to_csv(df: pd.DataFrame, logger: logging.Logger) -> Path:
    RAW_DATA_DIR.mkdir(parents=True, exist_ok=True)
    out_path = RAW_DATA_DIR / f"aod_ncr_{datetime.now():%Y-%m-%d}.csv"

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
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", help="YYYY-MM-DD, defaults to yesterday (UTC)")
    args = parser.parse_args()

    load_dotenv(PROJECT_ROOT / ".env")
    logger = setup_logging()

    if args.date:
        target_date = datetime.strptime(args.date, "%Y-%m-%d")
    else:
        target_date = datetime.now(timezone.utc) - timedelta(days=1)

    try:
        ee = init_earth_engine(logger)
    except Exception:
        logger.exception("Failed to initialize Earth Engine")
        return 1

    try:
        stations = load_station_points(logger)
    except RuntimeError:
        logger.exception("Failed to load station list")
        return 1

    if stations.empty:
        logger.warning("No stations to sample.")
        return 0

    try:
        df = sample_aod(ee, stations, target_date, logger)
    except Exception:
        logger.exception("AOD sampling failed")
        return 1

    if df.empty:
        logger.info("Nothing to write this run.")
        return 0

    append_to_csv(df, logger)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
