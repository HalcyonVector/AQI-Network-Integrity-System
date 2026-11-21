"""Request ERA5 for past burning seasons for the multi-season stubble test.

Same variables, area, pressure level and hours as the live pipeline (scripts/fetch_era5_wind.py), so the
history is directly comparable with 2026. Files go to data/historical/era5/ (never committed).

CDS queues every request for hours and limits how many run per dataset, so a request per month and year
(66 of them) would take days one after another. Instead each request covers ALL the requested years for
one month and one kind of data (a few thousand fields, well inside the per-request limit), which makes
the whole history 6 requests (3 kinds x October and November). They are submitted together and each is
saved as soon as it is ready. Files that already exist are skipped, so the script can be re-run.

    python historical/fetch_era5_history.py --dry-run
    python historical/fetch_era5_history.py
    python historical/fetch_era5_history.py --months 9 10 11 12      # add baseline months
    python historical/fetch_era5_history.py --years-per-request 4    # if CDS rejects a request as too large

Needs CDSAPI_URL and CDSAPI_KEY in .env and the ERA5 terms accepted, exactly as for the live pipeline.
"""
import argparse
import calendar
import shutil
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import fetch_era5_wind as live  # noqa: E402  (the live pipeline owns the dataset names, variables and areas)

OUT_DIR = ROOT / "data" / "historical" / "era5"
KINDS = ("single", "pressure", "precip")
DEFAULT_YEARS = list(range(2015, 2026))
DEFAULT_MONTHS = [10, 11]  # the burning season; add 9 and 12 as baselines


def out_path(kind: str, month: int, years: list[int], out_dir: Path = OUT_DIR) -> Path:
    return out_dir / f"era5_{kind}_{month:02d}_{min(years)}-{max(years)}.nc"


def build_request(kind: str, month: int, years: list[int]) -> tuple[str, dict]:
    """(dataset, request) for one month of the given years, mirroring the live per-day requests."""
    # days valid in EVERY requested year (a leap-year February keeps its 29th only if all years have it)
    n_days = min(calendar.monthrange(y, month)[1] for y in years)
    req = {"product_type": ["reanalysis"], "year": [str(y) for y in sorted(years)], "month": [f"{month:02d}"],
           "day": [f"{d:02d}" for d in range(1, n_days + 1)], "data_format": "netcdf", "download_format": "unarchived"}
    if kind == "single":
        return live.DATASET, {**req, "variable": live.VARIABLES, "time": live.HOURS, "area": live.AREA}
    if kind == "pressure":
        return live.PRESSURE_DATASET, {**req, "variable": live.PRESSURE_VARIABLES, "pressure_level": [live.PRESSURE_LEVEL],
                                       "time": live.HOURS, "area": live.AREA}
    if kind == "precip":
        return live.DATASET, {**req, "variable": [live.PRECIP_VARIABLE], "time": live.PRECIP_HOURS, "area": live.PRECIP_AREA}
    raise ValueError(f"unknown kind {kind!r}")


def field_count(request: dict) -> int:
    """Rough number of fields a request asks for (CDS rejects requests over its cost limit)."""
    n = 1
    for key in ("variable", "year", "month", "day", "time", "pressure_level"):
        n *= len(request.get(key, [1]))
    return n


def plan(years: list[int], months: list[int], years_per_request: int, out_dir: Path = OUT_DIR) -> list[tuple[str, int, list[int]]]:
    """(kind, month, years) requests whose output file does not exist yet, newest years first."""
    ys = sorted(years, reverse=True)
    groups = [sorted(ys[i:i + years_per_request]) for i in range(0, len(ys), years_per_request)]
    return [(k, m, g) for g in groups for m in months for k in KINDS if not out_path(k, m, g, out_dir).exists()]


def fetch_one(client, kind: str, month: int, years: list[int]) -> bool:
    dataset, request = build_request(kind, month, years)
    tmp = Path(tempfile.gettempdir()) / f"era5_hist_{kind}_{month:02d}_{min(years)}-{max(years)}.nc"
    print(f"requesting {kind} month {month:02d} years {min(years)}-{max(years)} ({field_count(request)} fields; queues for hours)", flush=True)
    try:
        client.retrieve(dataset, request).download(str(tmp))
        shutil.move(str(tmp), out_path(kind, month, years))
        print(f"  saved {out_path(kind, month, years).name}", flush=True)
        return True
    except Exception as exc:  # one failed request must not stop the others
        print(f"  failed {kind} {month:02d}: {exc}", flush=True)
        return False


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--years", type=int, nargs="+", default=DEFAULT_YEARS)
    ap.add_argument("--months", type=int, nargs="+", default=DEFAULT_MONTHS)
    ap.add_argument("--years-per-request", type=int, default=len(DEFAULT_YEARS))
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    todo = plan(args.years, args.months, args.years_per_request)
    print(f"{len(todo)} request(s) to make")
    if args.dry_run:
        for k, m, ys in todo:
            print(f"  {k} month {m:02d} years {min(ys)}-{max(ys)}: {field_count(build_request(k, m, ys)[1])} fields")
        return 0
    if not todo:
        return 0
    from dotenv import load_dotenv

    load_dotenv(ROOT / ".env")
    client = live.build_client()
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    with ThreadPoolExecutor(max_workers=len(todo)) as pool:  # all submitted at once, so they queue side by side
        results = list(pool.map(lambda j: fetch_one(client, *j), todo))
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())
