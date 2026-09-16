"""
Pull ERA5 10m wind vectors (u/v components) over the NCR + stubble-burning source
belt, for wind-conditioned neighbor consistency (component 3) and stubble-burning
transport attribution (component 4).

Usage:
    python scripts/fetch_era5_wind.py [--date YYYY-MM-DD]

Defaults to 6 days ago (UTC). ERA5 "final" reanalysis has ~5 day latency; requesting
anything more recent gets served from ERA5T (the preliminary/near-real-time version)
instead, which CDS marks with a separate `expver` dimension in the output and can
still be revised later. 6 days back stays safely in "final" territory. Pass --date
explicitly (allow-early) if you specifically want the (possibly-preliminary) recent
data.

Requires a Copernicus CDS account. See README.md "Component: ERA5 wind" for account
setup and -- important -- you must manually accept the ERA5 dataset's Terms of Use
on its CDS page before the API will serve you any data; this can't be done via the
API itself. Then set in .env:
    CDSAPI_URL=https://cds.climate.copernicus.eu/api
    CDSAPI_KEY=your-uid:your-api-key
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
from dotenv import load_dotenv

# Verified against https://cds.climate.copernicus.eu/datasets/reanalysis-era5-single-levels
# on 2026-09-17 -- re-check that page if requests start failing; CDS has renamed/
# restructured datasets before (the whole CDS-Beta migration in 2024).
DATASET = "reanalysis-era5-single-levels"
VARIABLES = ["10m_u_component_of_wind", "10m_v_component_of_wind"]

# North, West, South, East -- generous box covering the same stubble-burning source
# belt as the FIRMS pull (Punjab/Haryana/NCR/western UP), so wind vectors line up
# spatially with the fire detections for transport attribution.
AREA = [32.5, 73.5, 27.5, 81.0]

HOURS = [f"{h:02d}:00" for h in range(0, 24, 3)]  # 3-hourly, 00/03/06/09/12/15/18/21
FINAL_ERA5_LAG_DAYS = 6

PROJECT_ROOT = Path(__file__).resolve().parent.parent
RAW_DATA_DIR = PROJECT_ROOT / "data" / "raw"
LOG_DIR = PROJECT_ROOT / "logs"


def setup_logging() -> logging.Logger:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    log_path = LOG_DIR / f"fetch_era5_wind_{datetime.now():%Y-%m-%d}.log"
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[logging.FileHandler(log_path, encoding="utf-8"), logging.StreamHandler(sys.stdout)],
    )
    return logging.getLogger("fetch_era5_wind")


def build_client():
    import cdsapi

    url = os.environ.get("CDSAPI_URL")
    key = os.environ.get("CDSAPI_KEY")
    if not (url and key):
        raise RuntimeError(
            "CDSAPI_URL and CDSAPI_KEY must be set in .env. See README.md 'Component: ERA5 wind' for setup, "
            "including accepting the dataset's Terms of Use on the CDS website (required, can't be done via API)."
        )
    return cdsapi.Client(url=url, key=key)


def download_wind_nc(client, date: datetime, logger: logging.Logger) -> Path:
    request = {
        "product_type": ["reanalysis"],
        "variable": VARIABLES,
        "year": [str(date.year)],
        "month": [f"{date.month:02d}"],
        "day": [f"{date.day:02d}"],
        "time": HOURS,
        "area": AREA,
        "data_format": "netcdf",
        "download_format": "unarchived",
    }
    tmp_path = Path(tempfile.gettempdir()) / f"era5_wind_{date:%Y%m%d}.nc"
    logger.info("Requesting ERA5 wind for %s (this can queue for a while on CDS's end)", date.strftime("%Y-%m-%d"))
    client.retrieve(DATASET, request).download(str(tmp_path))
    logger.info("Downloaded %s", tmp_path)
    return tmp_path


def nc_to_dataframe(nc_path: Path, date: datetime, logger: logging.Logger) -> pd.DataFrame:
    # NetCDF, not GRIB: GRIB requires the cfgrib package, which needs the native
    # ecCodes C library -- no pip-installable wheel on Windows (confirmed by
    # testing: cfgrib imports but raises "Cannot find the ecCodes library" at
    # runtime). NetCDF avoids that entirely; netCDF4 ships its own binaries.
    import xarray as xr

    ds = xr.open_dataset(nc_path, engine="netcdf4")
    df = dataset_to_dataframe(ds, logger)
    logger.info("Parsed %d grid-point/time rows from NetCDF", len(df))
    return df


def dataset_to_dataframe(ds, logger: logging.Logger) -> pd.DataFrame:
    """Pure-ish function (takes an already-open xarray Dataset): the actual
    reshape/derive logic, split out from file I/O so it's testable against a
    synthetic in-memory Dataset without needing a real downloaded file."""
    import numpy as np

    # ERA5T (preliminary, <~5 days old) data carries an `expver` dimension
    # distinguishing it from final ERA5 -- at any given grid point/time only one
    # expver slice actually has data (the other is NaN), so skipna mean collapses
    # it to that one real value without needing the optional bottleneck/numbagg
    # packages that xarray's ffill() requires.
    if "expver" in ds.dims:
        ds = ds.mean("expver", skipna=True)
        logger.info("Response included multiple expver values (ERA5T/final overlap); collapsed via skipna mean.")

    df = ds.to_dataframe().reset_index()
    df = df.rename(columns={"u10": "u10_ms", "v10": "v10_ms"})

    keep_cols = [c for c in ("time", "latitude", "longitude", "u10_ms", "v10_ms") if c in df.columns]
    df = df[keep_cols].copy()

    df["wind_speed_ms"] = (df["u10_ms"] ** 2 + df["v10_ms"] ** 2) ** 0.5
    # Meteorological convention: direction wind is blowing FROM, 0=N, 90=E.
    df["wind_from_deg"] = (np.degrees(np.arctan2(-df["u10_ms"], -df["v10_ms"]))) % 360

    return df


def append_to_csv(df: pd.DataFrame, logger: logging.Logger) -> Path:
    RAW_DATA_DIR.mkdir(parents=True, exist_ok=True)
    out_path = RAW_DATA_DIR / f"era5_wind_{datetime.now():%Y-%m-%d}.csv"

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
    parser.add_argument("--date", help="YYYY-MM-DD, defaults to 6 days ago (UTC), safely inside final ERA5")
    args = parser.parse_args()

    load_dotenv(PROJECT_ROOT / ".env")
    logger = setup_logging()

    if args.date:
        target_date = datetime.strptime(args.date, "%Y-%m-%d")
    else:
        target_date = datetime.now(timezone.utc) - timedelta(days=FINAL_ERA5_LAG_DAYS)

    try:
        client = build_client()
    except RuntimeError:
        logger.exception("Failed to build CDS client")
        return 1

    nc_path = None
    try:
        nc_path = download_wind_nc(client, target_date, logger)
        df = nc_to_dataframe(nc_path, target_date, logger)
    except Exception:
        logger.exception("ERA5 wind fetch/parse failed")
        return 1
    finally:
        if nc_path is not None:
            nc_path.unlink(missing_ok=True)

    if df.empty:
        logger.warning("No wind data parsed for %s.", target_date.strftime("%Y-%m-%d"))
        return 0

    append_to_csv(df, logger)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
