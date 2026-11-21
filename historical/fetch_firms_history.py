"""Fetch past fire detections for the multi-season stubble test from the FIRMS standard-processing archive.

Same source region and API as the live pipeline (scripts/fetch_firms.py), but the standard-product
sources (_SP, the reprocessed archive) instead of the near-real-time ones:

  VIIRS_SNPP_SP    2012 onward  (the long, consistent record)
  VIIRS_NOAA20_SP  2018 onward  (second satellite, different overpass time)

One file per (source, year) goes to data/historical/firms/ (never committed). Existing files are
skipped, so the script can be re-run. Uses FIRMS_MAP_KEY from .env; the API allows 5000 requests per
10 minutes, and a full run is a few hundred.

    python historical/fetch_firms_history.py --dry-run
    python historical/fetch_firms_history.py
    python historical/fetch_firms_history.py --years 2019 2020 --months 9 10 11 12
"""
import argparse
import calendar
import logging
import os
import sys
from pathlib import Path

import pandas as pd
import requests

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import fetch_firms as live  # noqa: E402  (owns the API URL scheme, region and response checks)

OUT_DIR = ROOT / "data" / "historical" / "firms"
SOURCE_FIRST_YEAR = {"VIIRS_SNPP_SP": 2015, "VIIRS_NOAA20_SP": 2018}  # 2015 is the start of the backfill
DEFAULT_YEARS = list(range(2015, 2026))
DEFAULT_MONTHS = [10, 11]


def out_path(source: str, year: int, out_dir: Path = OUT_DIR) -> Path:
    return out_dir / f"firms_{source}_{year}.csv"


def month_chunks(year: int, months: list[int]) -> list[tuple[str, int]]:
    """(start_date, day_range) chunks of at most 5 days covering each requested month."""
    chunks: list[tuple[str, int]] = []
    for m in sorted(months):
        last = calendar.monthrange(year, m)[1]
        chunks += live.backfill_chunks(f"{year}-{m:02d}-01", f"{year}-{m:02d}-{last:02d}")
    return chunks


def missing_jobs(years: list[int], out_dir: Path = OUT_DIR) -> list[tuple[str, int]]:
    return [(s, y) for y in sorted(years) for s, first in SOURCE_FIRST_YEAR.items() if y >= first and not out_path(s, y, out_dir).exists()]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--years", type=int, nargs="+", default=DEFAULT_YEARS)
    ap.add_argument("--months", type=int, nargs="+", default=DEFAULT_MONTHS)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    todo = missing_jobs(args.years)
    print(f"{len(todo)} file(s) missing")
    if args.dry_run:
        for s, y in todo:
            print(f"  {s} {y}: {len(month_chunks(y, args.months))} requests")
        return 0
    from dotenv import load_dotenv

    load_dotenv(ROOT / ".env")
    key = os.environ.get("FIRMS_MAP_KEY")
    if not key:
        print("FIRMS_MAP_KEY not set in .env", file=sys.stderr)
        return 1
    logger = logging.getLogger("firms_history")
    session = requests.Session()
    session.headers.update({"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"})
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    failed = 0
    for source, year in todo:
        try:
            frames = [live.fetch_one_source(key, source, session, logger, days, start) for start, days in month_chunks(year, args.months)]
            frames = [f for f in frames if not f.empty]
            df = pd.concat(frames, ignore_index=True).drop_duplicates() if frames else pd.DataFrame()
            if df.empty:  # an empty season is suspicious: do not write a file that would hide the hole
                raise ValueError("no detections returned")
            df.to_csv(out_path(source, year), index=False)
            print(f"{source} {year}: {len(df)} detections", flush=True)
        except Exception as exc:
            failed += 1
            print(f"{source} {year} failed: {str(exc).replace(key, 'REDACTED')}", flush=True)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
