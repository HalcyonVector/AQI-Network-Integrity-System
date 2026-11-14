"""Fill days that have no file, for the sources that can be fetched after the fact.

The daily fetchers only re-process a short trailing window, so if the laptop is off for longer than
that, the days in between stay empty. This finds every hole between a source's first and newest file
and asks that source for exactly those days. It runs as part of the daily driver, so a missed day is
repaired the next time the machine is on.

Not recoverable: the CPCB feed (data.gov.in serves only the current snapshot, never history). For
those days the OpenAQ archive of the same stations is the fallback.

    python scripts/backfill_missing.py            # fill what is missing
    python scripts/backfill_missing.py --dry-run  # just list the holes
"""
import argparse
import re
import subprocess
import sys
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RAW_DIR = ROOT / "data" / "raw"
# Slow or quota-bound sources are capped per run so one long outage cannot hold up the daily run.
MAX_DAYS_PER_RUN = {"openaq_hourly": 60, "firms_acq": 60, "aod_ncr": 6, "era5_wind": 3}


def have_dates(prefix: str, raw_dir: Path = RAW_DIR) -> set[date]:
    out = set()
    for f in raw_dir.glob(f"{prefix}_*.csv"):
        m = re.search(r"(\d{4}-\d{2}-\d{2})\.csv$", f.name)
        if m:
            out.add(date.fromisoformat(m.group(1)))
    return out


def holes(prefix: str, raw_dir: Path = RAW_DIR) -> list[date]:
    """Dates strictly between the first and newest file that have no file."""
    have = have_dates(prefix, raw_dir)
    if len(have) < 2:
        return []
    first, last = min(have), max(have)
    return [first + timedelta(d) for d in range((last - first).days + 1) if first + timedelta(d) not in have]


def runs_of_days(days: list[date]) -> list[tuple[date, date]]:
    """Group sorted dates into contiguous (start, end) ranges."""
    out: list[tuple[date, date]] = []
    for d in sorted(days):
        if out and d - out[-1][1] == timedelta(1):
            out[-1] = (out[-1][0], d)
        else:
            out.append((d, d))
    return out


def commands(prefix: str, days: list[date]) -> list[list[str]]:
    """The fetcher invocations that cover these days (newest first, so recent days win under the cap)."""
    days = sorted(days, reverse=True)[: MAX_DAYS_PER_RUN.get(prefix, 5)]
    py = sys.executable
    if prefix in ("openaq_hourly", "firms_acq"):
        script = "fetch_openaq.py" if prefix == "openaq_hourly" else "fetch_firms.py"
        return [[py, str(ROOT / "scripts" / script), "--start", str(a), "--end", str(b)] for a, b in runs_of_days(days)]
    script = "fetch_aod.py" if prefix == "aod_ncr" else "fetch_era5_wind.py"
    return [[py, str(ROOT / "scripts" / script), "--date", str(d)] for d in days]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    failed = 0
    cpcb = holes("cpcb_ncr")
    if cpcb:
        print(f"cpcb_ncr: {len(cpcb)} missing day(s), not recoverable (live-only feed); OpenAQ covers these stations")
    for prefix in ("openaq_hourly", "firms_acq", "aod_ncr", "era5_wind"):
        missing = holes(prefix)
        if not missing:
            print(f"{prefix}: no holes")
            continue
        print(f"{prefix}: {len(missing)} missing day(s): {', '.join(str(d) for d in missing[:12])}{' ...' if len(missing) > 12 else ''}")
        for cmd in commands(prefix, missing):
            print("  ->", " ".join(Path(c).name if i == 0 or i == 1 else c for i, c in enumerate(cmd)))
            if not args.dry_run and subprocess.run(cmd, cwd=ROOT).returncode != 0:
                failed += 1
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
