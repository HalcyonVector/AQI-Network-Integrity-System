"""Download the yearly CPCB station files (15-minute concentrations, 2015-2025) from the
Vonter/india-cpcb-aqi GitHub releases, which republish CPCB's own data repository (ODbL 1.0: credit the
source and, if a derived database is published, share it alike).

One Parquet file per year, about 3.9 GB in all, goes to data/historical/bronze/cpcb/ (never committed).
Existing files are skipped, so the script can be re-run after an interruption. Needs the GitHub CLI (`gh`).

    python historical/fetch_cpcb_history.py --dry-run
    python historical/fetch_cpcb_history.py
    python historical/fetch_cpcb_history.py --years 2019 2020
"""
import argparse
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT_DIR = ROOT / "data" / "historical" / "bronze" / "cpcb"
REPO = "Vonter/india-cpcb-aqi"
DEFAULT_YEARS = list(range(2015, 2026))


def asset_name(year: int) -> str:
    return f"cpcb-air-quality-{year}.parquet"


def missing_years(years: list[int], out_dir: Path = OUT_DIR) -> list[int]:
    return [y for y in sorted(years) if not (out_dir / asset_name(y)).exists()]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--years", type=int, nargs="+", default=DEFAULT_YEARS)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    todo = missing_years(args.years)
    print(f"{len(todo)} file(s) missing: {todo}")
    if args.dry_run or not todo:
        return 0
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    failed = 0
    for year in todo:
        # download to a temporary name so an interrupted transfer never looks like a finished file
        part = OUT_DIR / (asset_name(year) + ".part")
        part.unlink(missing_ok=True)
        cmd = ["gh", "release", "download", str(year), "--repo", REPO, "--pattern", asset_name(year), "--output", str(part)]
        print(f"downloading {asset_name(year)}", flush=True)
        if subprocess.run(cmd).returncode != 0 or not part.exists():
            failed += 1
            print(f"  failed: {year}", flush=True)
            continue
        part.rename(OUT_DIR / asset_name(year))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
