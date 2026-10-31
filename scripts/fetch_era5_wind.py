"""
Pull ERA5 10m wind vectors (u/v components) over the NCR + stubble-burning source
belt, for wind-conditioned neighbor consistency (component 3) and stubble-burning
transport attribution (component 4).

Usage:
    python scripts/fetch_era5_wind.py [--date YYYY-MM-DD]

Defaults to 6 days ago (UTC), the point where data reliably exists: ERA5 is published
with ~5 days latency as ERA5T (preliminary), and is only replaced by the final ERA5
product months later. So data pulled by this script is normally PRELIMINARY -- the
`expver` column says which: "0005" = ERA5T (revisable), "0001" = final. (Confirmed on
the first live run: a 7-day-old date came back expver 0005.) Differences between
ERA5T and final are usually small, but don't assume they're zero.

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
from datewin import trailing_window

# Verified against https://cds.climate.copernicus.eu/datasets/reanalysis-era5-single-levels
# on 2026-09-17 -- re-check that page if requests start failing; CDS has renamed/
# restructured datasets before (the whole CDS-Beta migration in 2024).
DATASET = "reanalysis-era5-single-levels"
# 10 m wind (the original pull) plus the two variables the stubble analysis lacked: boundary-layer
# height (the confounder it could not control for) and 2 m temperature.
VARIABLES = ["10m_u_component_of_wind", "10m_v_component_of_wind", "boundary_layer_height", "2m_temperature"]

# Transport-layer wind. 10 m wind badly understates how fast air actually moves regional smoke, which
# forced an arbitrary "speed factor" in the attribution. 925 hPa (~750 m, inside the daytime boundary
# layer over the Indo-Gangetic Plain) is a standard choice for regional transport. Chosen a priori.
PRESSURE_DATASET = "reanalysis-era5-pressure-levels"
PRESSURE_VARIABLES = ["u_component_of_wind", "v_component_of_wind"]
PRESSURE_LEVEL = "925"

# North, West, South, East -- generous box covering the same stubble-burning source
# belt as the FIRMS pull (Punjab/Haryana/NCR/western UP), so wind vectors line up
# spatially with the fire detections for transport attribution.
AREA = [32.5, 73.5, 27.5, 81.0]

# Rain, the leading unexplained alternative to stubble for Delhi's late-September clean-to-dirty step
# (wet scavenging ends as the monsoon withdraws). Precipitation must be summed over ALL hours of a
# day, so it is requested hourly and only for a box around Delhi (a small, fast download), and kept
# in its own file rather than bloating the 3-hourly regional grid.
PRECIP_VARIABLE = "total_precipitation"
PRECIP_AREA = [29.2, 76.6, 28.0, 77.8]  # North, West, South, East around Delhi
PRECIP_HOURS = [f"{h:02d}:00" for h in range(24)]

HOURS = [f"{h:02d}:00" for h in range(0, 24, 3)]  # 3-hourly, 00/03/06/09/12/15/18/21
# ERA5T first appears ~5 days after the fact; re-process a trailing window so a late or
# skipped day is picked up on a later run. CDS caches identical requests, so repeats are cheap.
WINDOW_NEWEST_DAYS_BACK = 5
WINDOW_SPAN_DAYS = 8

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


def download_pressure_nc(client, date: datetime, logger: logging.Logger) -> Path:
    request = {
        "product_type": ["reanalysis"],
        "variable": PRESSURE_VARIABLES,
        "pressure_level": [PRESSURE_LEVEL],
        "year": [str(date.year)],
        "month": [f"{date.month:02d}"],
        "day": [f"{date.day:02d}"],
        "time": HOURS,
        "area": AREA,
        "data_format": "netcdf",
        "download_format": "unarchived",
    }
    tmp_path = Path(tempfile.gettempdir()) / f"era5_{PRESSURE_LEVEL}hpa_{date:%Y%m%d}.nc"
    logger.info("Requesting ERA5 %s hPa wind for %s", PRESSURE_LEVEL, date.strftime("%Y-%m-%d"))
    client.retrieve(PRESSURE_DATASET, request).download(str(tmp_path))
    logger.info("Downloaded %s", tmp_path)
    return tmp_path


def merge_levels(single: pd.DataFrame, pressure: pd.DataFrame, logger: logging.Logger) -> pd.DataFrame:
    """Join the pressure-level columns onto the single-level frame by (time, lat, lon). Fails if the
    two grids disagree rather than silently writing holes into the transport-layer wind."""
    pressure = pressure.drop(columns=[c for c in ("expver",) if c in pressure.columns])
    merged = single.merge(pressure, on=["time", "latitude", "longitude"], how="left")
    missing = int(merged[[c for c in pressure.columns if c.endswith("925_ms")]].isna().any(axis=1).sum())
    if missing:
        raise ValueError(f"{missing} of {len(merged)} grid points have no {PRESSURE_LEVEL} hPa wind after merging")
    logger.info("Merged %s hPa wind onto %d single-level rows", PRESSURE_LEVEL, len(merged))
    return merged


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
    # The CDS NetCDF names its time coordinate `valid_time`, not `time` (found on
    # the first live run: a `time`-only column filter silently wrote every row
    # with no timestamp). Fail loudly if neither is present rather than dropping it.
    renames = {"u10": "u10_ms", "v10": "v10_ms", "blh": "blh_m", "t2m": "t2m_k", "valid_time": "time"}
    if "pressure_level" in df.columns:  # a pressure-level file: its u/v are the transport-layer wind
        level = int(df["pressure_level"].iloc[0])
        renames.update({"u": f"u{level}_ms", "v": f"v{level}_ms"})
        df = df.drop(columns="pressure_level")
    df = df.rename(columns=renames)
    if "time" not in df.columns:
        raise ValueError(f"ERA5 response has no recognizable time coordinate; columns: {df.columns.tolist()}")

    # `expver` marks final ERA5 ("0001") vs preliminary ERA5T ("0005", revisable);
    # in the real file it is a per-timestep coordinate, so keep it as a column.
    wanted = ("time", "latitude", "longitude", "u10_ms", "v10_ms", "blh_m", "t2m_k", "u925_ms", "v925_ms", "expver")
    df = df[[c for c in wanted if c in df.columns]].copy()

    if "u10_ms" in df.columns:
        df["wind_speed_ms"] = (df["u10_ms"] ** 2 + df["v10_ms"] ** 2) ** 0.5
        # Meteorological convention: direction wind is blowing FROM, 0=N, 90=E.
        df["wind_from_deg"] = (np.degrees(np.arctan2(-df["u10_ms"], -df["v10_ms"]))) % 360

    return df


def download_precip_nc(client, date: datetime, logger: logging.Logger) -> Path:
    request = {
        "product_type": ["reanalysis"],
        "variable": [PRECIP_VARIABLE],
        "year": [str(date.year)],
        "month": [f"{date.month:02d}"],
        "day": [f"{date.day:02d}"],
        "time": PRECIP_HOURS,
        "area": PRECIP_AREA,
        "data_format": "netcdf",
        "download_format": "unarchived",
    }
    tmp_path = Path(tempfile.gettempdir()) / f"era5_precip_{date:%Y%m%d}.nc"
    logger.info("Requesting ERA5 hourly precipitation around Delhi for %s", date.strftime("%Y-%m-%d"))
    client.retrieve(DATASET, request).download(str(tmp_path))
    return tmp_path


def precip_dataset_to_hourly(ds, logger: logging.Logger) -> pd.DataFrame:
    """Hourly precipitation over the Delhi box from an open ERA5 Dataset: area mean and area max, in mm.

    ERA5's `tp` is metres of water accumulated over the hour ENDING at each timestamp, so the value
    labelled 06:00 UTC is the rain that fell 05:00-06:00. Summing 24 consecutive hourly values gives a day.
    """
    if "expver" in ds.dims:
        ds = ds.mean("expver", skipna=True)
    df = ds.to_dataframe().reset_index().rename(columns={"valid_time": "time"})
    if "time" not in df.columns or "tp" not in df.columns:
        raise ValueError(f"unexpected precipitation response; columns: {df.columns.tolist()}")
    df["precip_mm"] = (df["tp"] * 1000.0).clip(lower=0.0)  # tiny negative values are float noise in ERA5
    out = df.groupby("time").agg(precip_mm_delhi_mean=("precip_mm", "mean"), precip_mm_delhi_max=("precip_mm", "max")).reset_index()
    return out.round({"precip_mm_delhi_mean": 4, "precip_mm_delhi_max": 4})


def precip_file_complete(date: datetime) -> bool:
    """True if a full 24-hour precipitation file already exists for the date, so the daily window does
    not ask CDS for it again on every run."""
    path = RAW_DATA_DIR / f"era5_precip_{date:%Y-%m-%d}.csv"
    return path.exists() and sum(1 for _ in open(path, encoding="utf-8")) - 1 >= len(PRECIP_HOURS)


def wind_file_complete(date: datetime) -> bool:
    """True if the date's wind file already holds every expected timestamp. Without this the trailing
    window re-requested all 8 days (2 CDS jobs each) on every run, so one run could queue for hours and
    outlast the scheduled task's time limit. Use --force to re-pull (e.g. when ERA5T is replaced by the
    final product)."""
    path = RAW_DATA_DIR / f"era5_wind_{date:%Y-%m-%d}.csv"
    if not path.exists():
        return False
    try:
        return pd.read_csv(path, usecols=["time"])["time"].nunique() >= len(HOURS)
    except Exception:
        return False


def fetch_and_write_precip(client, date: datetime, logger: logging.Logger) -> Path | None:
    """Best-effort: rain is a control variable, so failing to get it must never block the wind data."""
    if precip_file_complete(date):
        return RAW_DATA_DIR / f"era5_precip_{date:%Y-%m-%d}.csv"
    path = None
    try:
        import xarray as xr

        path = download_precip_nc(client, date, logger)
        hourly = precip_dataset_to_hourly(xr.open_dataset(path, engine="netcdf4"), logger)
        RAW_DATA_DIR.mkdir(parents=True, exist_ok=True)
        out_path = RAW_DATA_DIR / f"era5_precip_{date:%Y-%m-%d}.csv"
        hourly.to_csv(out_path, index=False)
        logger.info("Wrote %d hourly precipitation rows to %s", len(hourly), out_path.name)
        return out_path
    except Exception:
        logger.exception("Precipitation fetch failed for %s (non-fatal)", date.strftime("%Y-%m-%d"))
        return None
    finally:
        if path is not None:
            path.unlink(missing_ok=True)


def warn_if_partial_day(df: pd.DataFrame, date: datetime, logger: logging.Logger) -> bool:
    """ERA5T publishes a day's hours progressively, so the newest date in the window can
    come back with only some of its timestamps (seen: 5 of 8 for 2026-10-11 on 2026-10-16).
    The file is still written -- the next run overwrites it with the full day -- but
    anything reading it before then should know it is incomplete."""
    got, expected = df["time"].nunique(), len(HOURS)
    if got < expected:
        logger.warning("PARTIAL DAY %s: %d of %d timestamps published so far; will be completed on a later run",
                       date.strftime("%Y-%m-%d"), got, expected)
        return True
    return False


def write_date_file(df: pd.DataFrame, date: datetime, logger: logging.Logger) -> Path:
    """One file per OBSERVATION date, overwritten on re-run, so backfills and repeated
    runs are idempotent (it used to append to a file named for the fetch date)."""
    RAW_DATA_DIR.mkdir(parents=True, exist_ok=True)
    out_path = RAW_DATA_DIR / f"era5_wind_{date:%Y-%m-%d}.csv"

    df = df.copy()
    df.insert(0, "fetched_at_utc", datetime.now(timezone.utc).isoformat(timespec="seconds"))
    df.to_csv(out_path, index=False)
    logger.info("Wrote %d rows to %s", len(df), out_path.name)
    return out_path

def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", help="YYYY-MM-DD; default: re-process the trailing window of recent dates")
    parser.add_argument("--force", action="store_true", help="re-request dates whose wind file is already complete")
    args = parser.parse_args()

    load_dotenv(PROJECT_ROOT / ".env")
    logger = setup_logging()

    if args.date:
        dates = [datetime.strptime(args.date, "%Y-%m-%d")]
    else:
        dates = trailing_window(datetime.now(timezone.utc), WINDOW_NEWEST_DAYS_BACK, WINDOW_SPAN_DAYS)

    try:
        client = build_client()
    except RuntimeError:
        logger.exception("Failed to build CDS client")
        return 1

    failures = 0
    for target_date in dates:
        if not args.force and wind_file_complete(target_date):
            logger.info("%s: wind file already complete, skipping", target_date.strftime("%Y-%m-%d"))
            fetch_and_write_precip(client, target_date, logger)  # no-op if the precipitation file is complete too
            continue
        paths = []
        try:
            paths.append(download_wind_nc(client, target_date, logger))
            single = nc_to_dataframe(paths[-1], target_date, logger)
            paths.append(download_pressure_nc(client, target_date, logger))
            df = merge_levels(single, nc_to_dataframe(paths[-1], target_date, logger), logger)
        except Exception:
            logger.exception("ERA5 fetch/parse failed for %s", target_date.strftime("%Y-%m-%d"))
            failures += 1
            continue
        finally:
            for path in paths:
                path.unlink(missing_ok=True)

        if df.empty:
            logger.warning("No wind data parsed for %s.", target_date.strftime("%Y-%m-%d"))
            continue
        warn_if_partial_day(df, target_date, logger)
        write_date_file(df, target_date, logger)
        fetch_and_write_precip(client, target_date, logger)
    return 1 if failures == len(dates) else 0


if __name__ == "__main__":
    raise SystemExit(main())
