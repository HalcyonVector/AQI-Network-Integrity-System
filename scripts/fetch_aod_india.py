"""
MODIS MAIAC AOD at every station of the national hourly record (about 500 CPCB stations), for the national
satellite check. Same granule masking as scripts/fetch_aod.py (each granule's own QA band; clear, best quality
pixels only), sampled at the station master's coordinates for stations with an OpenAQ id.

    python scripts/fetch_aod_india.py                              # trailing window, idempotent
    python scripts/fetch_aod_india.py --start 2026-09-23 --end 2026-11-15   # backfill

Output: data/raw/aod_india_YYYY-MM-DD.parquet, one file per observation date, overwritten on re-run.
Columns: openaq_id, aod_055 (NaN where the satellite saw no clear pixel). Requires the same Earth Engine setup as
fetch_aod.py (GEE_PROJECT in .env).
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).resolve().parent))
import fetch_aod as fa  # noqa: E402
from datewin import trailing_window  # noqa: E402

PROJECT_ROOT = Path(__file__).resolve().parent.parent
RAW = PROJECT_ROOT / "data" / "raw"
MASTER = PROJECT_ROOT / "data" / "processed" / "historical" / "station_master.csv"
INDIA_BOUNDS = (68.0, 6.0, 98.0, 37.5)


def load_points() -> pd.DataFrame:
    m = pd.read_csv(MASTER).dropna(subset=["openaq_id", "latitude", "longitude"])
    m = m.assign(station=m["openaq_id"].astype(int).astype(str)).drop_duplicates("station")
    return m[["station", "city", "latitude", "longitude"]]


def needs(day: datetime, explicit: bool) -> bool:
    return explicit or not (RAW / f"aod_india_{day:%Y-%m-%d}.parquet").exists() or (datetime.now(timezone.utc).replace(tzinfo=None) - day).days <= 14


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--start")
    ap.add_argument("--end")
    args = ap.parse_args()
    load_dotenv(PROJECT_ROOT / ".env")
    logger = fa.setup_logging()
    if args.start or args.end:
        s = datetime.strptime(args.start or args.end, "%Y-%m-%d")
        e = datetime.strptime(args.end or args.start, "%Y-%m-%d")
        dates = [s + timedelta(days=i) for i in range((e - s).days + 1)]
    else:
        dates = trailing_window(datetime.now(timezone.utc), fa.WINDOW_NEWEST_DAYS_BACK, fa.WINDOW_SPAN_DAYS)
    explicit = bool(args.start or args.end)
    try:
        ee = fa.init_earth_engine(logger)
    except Exception:
        logger.exception("Failed to initialize Earth Engine")
        return 1
    points = load_points()
    failures = 0
    for d in dates:
        if not needs(d, explicit):
            continue
        try:
            df = fa.sample_aod(ee, points, d, logger, bounds=INDIA_BOUNDS)
        except Exception:
            logger.exception("AOD sampling failed for %s", d.strftime("%Y-%m-%d"))
            failures += 1
            continue
        if df.empty:
            continue
        out = pd.DataFrame({"openaq_id": df["station"].astype("int32"), "aod_055": df["aod_055"].astype("float32")})
        path = RAW / f"aod_india_{d:%Y-%m-%d}.parquet"
        out.to_parquet(path.with_suffix(".part"), index=False, compression="zstd")
        path.with_suffix(".part").replace(path)
        logger.info("%s: %d stations, %d with a valid pixel", path.name, len(out), int(out["aod_055"].notna().sum()))
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
