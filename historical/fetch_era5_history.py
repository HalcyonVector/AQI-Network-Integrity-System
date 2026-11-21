"""Request ERA5 for past burning seasons, one file per (kind, year, month), for the multi-season stubble test.

Same variables, area, pressure level and hours as the live pipeline (scripts/fetch_era5_wind.py), so the
history is directly comparable with 2026. Files go to data/historical/era5/ (never committed).

CDS queues requests for hours, so a run does at most --max-requests of them, newest season first, skips
files that already exist, and can simply be run again (daily is fine) until nothing is left.

    python historical/fetch_era5_history.py --dry-run             # list what is missing
    python historical/fetch_era5_history.py --max-requests 6      # request up to 6 files
    python historical/fetch_era5_history.py --years 2018 2019 --months 9 10 11 12

Needs CDSAPI_URL and CDSAPI_KEY in .env and the ERA5 terms accepted, exactly as for the live pipeline.
"""
import argparse
import calendar
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import fetch_era5_wind as live  # noqa: E402  (the live pipeline owns the dataset names, variables and areas)

OUT_DIR = ROOT / "data" / "historical" / "era5"
KINDS = ("single", "pressure", "precip")
DEFAULT_YEARS = list(range(2015, 2026))
DEFAULT_MONTHS = [10, 11]  # the burning season; pass 9 12 as well for baselines


def out_path(kind: str, year: int, month: int, out_dir: Path = OUT_DIR) -> Path:
    return out_dir / f"era5_{kind}_{year}-{month:02d}.nc"


def build_request(kind: str, year: int, month: int) -> tuple[str, dict]:
    """(dataset, request) for one month, mirroring the live per-day requests."""
    days = [f"{d:02d}" for d in range(1, calendar.monthrange(year, month)[1] + 1)]
    req = {"product_type": ["reanalysis"], "year": [str(year)], "month": [f"{month:02d}"], "day": days,
           "data_format": "netcdf", "download_format": "unarchived"}
    if kind == "single":
        return live.DATASET, {**req, "variable": live.VARIABLES, "time": live.HOURS, "area": live.AREA}
    if kind == "pressure":
        return live.PRESSURE_DATASET, {**req, "variable": live.PRESSURE_VARIABLES, "pressure_level": [live.PRESSURE_LEVEL],
                                       "time": live.HOURS, "area": live.AREA}
    if kind == "precip":
        return live.DATASET, {**req, "variable": [live.PRECIP_VARIABLE], "time": live.PRECIP_HOURS, "area": live.PRECIP_AREA}
    raise ValueError(f"unknown kind {kind!r}")


def missing_jobs(years: list[int], months: list[int], out_dir: Path = OUT_DIR) -> list[tuple[str, int, int]]:
    """Files still to fetch, newest season first (recent years matter most if the run is cut short)."""
    return [(k, y, m) for y in sorted(years, reverse=True) for m in months for k in KINDS if not out_path(k, y, m, out_dir).exists()]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--years", type=int, nargs="+", default=DEFAULT_YEARS)
    ap.add_argument("--months", type=int, nargs="+", default=DEFAULT_MONTHS)
    ap.add_argument("--max-requests", type=int, default=6)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    todo = missing_jobs(args.years, args.months)
    print(f"{len(todo)} file(s) missing; this run: {min(len(todo), args.max_requests)}")
    if args.dry_run:
        for k, y, m in todo:
            print(f"  {k} {y}-{m:02d}")
        return 0
    if not todo:
        return 0
    from dotenv import load_dotenv

    load_dotenv(ROOT / ".env")
    client = live.build_client()
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    failed = 0
    for kind, year, month in todo[: args.max_requests]:
        dataset, request = build_request(kind, year, month)
        tmp = Path(tempfile.gettempdir()) / f"era5_hist_{kind}_{year}{month:02d}.nc"
        print(f"requesting {kind} {year}-{month:02d} (may queue for hours)", flush=True)
        try:
            client.retrieve(dataset, request).download(str(tmp))
            shutil.move(str(tmp), out_path(kind, year, month))
        except Exception as exc:  # one failed month must not stop the rest
            failed += 1
            print(f"  failed: {exc}", flush=True)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
