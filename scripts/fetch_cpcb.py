"""
Pull hourly CAAQMS station data (PM2.5, PM10, NO2, SO2, CO, O3) for the Delhi NCR
region (Delhi, Noida, Ghaziabad, Gurugram, Faridabad) from data.gov.in's
"Real Time Air Quality Index from various locations" API, sourced from CPCB.

Must be run from a residential IP -- data.gov.in / CPCB endpoints block
datacenter/cloud egress ranges outright.

Usage:
    python scripts/fetch_cpcb.py

Requires CPCB_API_KEY in the environment or a .env file (see .env.example).
Get a free key by registering at https://data.gov.in and visiting the API
tab of the "Real time Air Quality Index from various locations" resource.
"""

from __future__ import annotations

import logging
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import requests
from dotenv import load_dotenv

# Verified against the data.gov.in catalog listing for
# "Real time Air Quality Index from various locations" on 2026-09-17.
# The catalog's resource ID can change if CPCB republishes the dataset --
# re-check https://www.data.gov.in/catalog/real-time-air-quality-index
# (Data tab -> API) if this endpoint starts 404ing.
DEFAULT_RESOURCE_ID = "3b01bcb8-0b14-4abf-b6f2-c1bfd384ba69"
API_BASE = "https://api.data.gov.in/resource"

NCR_CITIES = {"Delhi", "Noida", "Ghaziabad", "Gurugram", "Faridabad"}
TARGET_POLLUTANTS = {"PM2.5", "PM10", "NO2", "SO2", "CO", "OZONE"}

PAGE_LIMIT = 1000
MAX_RETRIES = 4
RETRY_BACKOFF_SECS = 5
REQUEST_TIMEOUT_SECS = 30

PROJECT_ROOT = Path(__file__).resolve().parent.parent
RAW_DATA_DIR = PROJECT_ROOT / "data" / "raw"
LOG_DIR = PROJECT_ROOT / "logs"


def setup_logging() -> logging.Logger:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    log_path = LOG_DIR / f"fetch_{datetime.now():%Y-%m-%d}.log"
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[logging.FileHandler(log_path, encoding="utf-8"), logging.StreamHandler(sys.stdout)],
    )
    return logging.getLogger("fetch_cpcb")


def fetch_all_records(api_key: str, resource_id: str, logger: logging.Logger) -> list[dict]:
    records: list[dict] = []
    offset = 0
    session = requests.Session()
    # The API hangs (read-timeout, no response at all) on requests without a
    # browser-like User-Agent -- default python-requests/urllib UA gets silently
    # stalled server-side. Confirmed by testing: identical request succeeds
    # instantly once this header is set.
    session.headers.update(
        {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
            ),
            "Accept": "application/json",
        }
    )

    while True:
        params = {
            "api-key": api_key,
            "format": "json",
            "limit": PAGE_LIMIT,
            "offset": offset,
        }
        url = f"{API_BASE}/{resource_id}"

        for attempt in range(1, MAX_RETRIES + 1):
            try:
                resp = session.get(url, params=params, timeout=REQUEST_TIMEOUT_SECS)
                resp.raise_for_status()
                payload = resp.json()
                break
            except requests.RequestException as exc:
                logger.warning("Request failed (attempt %d/%d, offset %d): %s", attempt, MAX_RETRIES, offset, exc)
                if attempt == MAX_RETRIES:
                    logger.error(
                        "Giving up after %d attempts at offset %d. If this is a connection "
                        "reset/403, you may be hitting from a blocked (datacenter) IP -- this "
                        "must run from a residential connection.",
                        MAX_RETRIES,
                        offset,
                    )
                    raise
                time.sleep(RETRY_BACKOFF_SECS * attempt)

        page_records = payload.get("records", [])
        if not page_records:
            break

        records.extend(page_records)
        logger.info("Fetched %d records at offset %d (running total: %d)", len(page_records), offset, len(records))

        total = payload.get("total")
        offset += len(page_records)
        if total is not None and offset >= int(total):
            break
        if len(page_records) < PAGE_LIMIT:
            break

    return records


def filter_and_reshape(records: list[dict], logger: logging.Logger) -> pd.DataFrame:
    if not records:
        return pd.DataFrame()

    df = pd.DataFrame.from_records(records)

    # The API's display names (pollutant_min/max/avg) don't match the actual JSON
    # keys it returns (min_value/max_value/avg_value) -- confirmed by inspecting a
    # live response; the "field" metadata block's `name` differs from its `id`.
    missing_cols = {"city", "station", "pollutant_id", "avg_value"} - set(df.columns)
    if missing_cols:
        logger.error("API response is missing expected columns: %s", missing_cols)
        raise ValueError(f"Unexpected API schema, missing columns: {missing_cols}")

    df = df[df["city"].isin(NCR_CITIES)].copy()
    df = df[df["pollutant_id"].isin(TARGET_POLLUTANTS)].copy()
    if df.empty:
        logger.warning("No NCR records matched the target cities/pollutants for this pull.")
        return df

    for col in ("min_value", "max_value", "avg_value"):
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")

    index_cols = [c for c in ("country", "state", "city", "station", "latitude", "longitude", "last_update") if c in df.columns]

    wide = df.pivot_table(
        index=index_cols,
        columns="pollutant_id",
        values="avg_value",
        aggfunc="first",
    ).reset_index()
    wide.columns.name = None

    wide.insert(0, "fetched_at_utc", datetime.now(timezone.utc).isoformat(timespec="seconds"))

    return wide


def append_to_csv(df: pd.DataFrame, logger: logging.Logger) -> Path:
    RAW_DATA_DIR.mkdir(parents=True, exist_ok=True)
    out_path = RAW_DATA_DIR / f"cpcb_ncr_{datetime.now():%Y-%m-%d}.csv"

    write_header = not out_path.exists()
    df.to_csv(out_path, mode="a", header=write_header, index=False)
    logger.info("Appended %d rows to %s", len(df), out_path)
    return out_path


def main() -> int:
    load_dotenv(PROJECT_ROOT / ".env")
    logger = setup_logging()

    api_key = os.environ.get("CPCB_API_KEY")
    if not api_key:
        logger.error("CPCB_API_KEY not set. Copy .env.example to .env and fill in your data.gov.in API key.")
        return 1

    resource_id = os.environ.get("CPCB_RESOURCE_ID", DEFAULT_RESOURCE_ID)

    try:
        records = fetch_all_records(api_key, resource_id, logger)
    except requests.RequestException:
        return 1

    logger.info("Fetched %d total records from data.gov.in", len(records))

    df = filter_and_reshape(records, logger)
    if df.empty:
        logger.warning("Nothing to write this run.")
        return 0

    append_to_csv(df, logger)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
