# NCR AQI Network Integrity System

Layered system over Delhi NCR's CAAQMS network (Delhi, Noida, Gurugram, Ghaziabad,
Faridabad) that will eventually:

1. Flag stations whose reported PM2.5 drifts from a satellite-AOD-based independent estimate.
2. Fill gaps in reported data using that same satellite-based estimate.
3. Cross-check stations against wind-conditioned neighbor consistency to catch faults
   during nighttime/cloud cover when satellite can't see.
4. Attribute PM2.5 spikes to regional stubble-burning transport (NASA FIRMS fire data +
   wind vectors) versus local sources.

CPCB station data and NASA FIRMS fire detections are built so far. Satellite AOD
(Google Earth Engine), ERA5 wind/met (Copernicus CDS), and the modeling/dashboard
layers are not started.

## Component: CPCB fetch script

`scripts/fetch_cpcb.py` pulls hourly PM2.5, PM10, NO2, SO2, CO, O3 for CAAQMS stations
in the five NCR cities from data.gov.in's "Real time Air Quality Index from various
locations" API (sourced from CPCB), and appends one row per station per timestamp to a
dated CSV in `data/raw/`.

### Setup

1. Register for a free API key at [data.gov.in](https://www.data.gov.in) (Register ->
   confirm email), then open the
   [Real time Air Quality Index](https://www.data.gov.in/catalog/real-time-air-quality-index)
   catalog entry -> **API** tab -> **Generate API Key**.
2. Copy `.env.example` to `.env` and paste your key into `CPCB_API_KEY`.
3. `python -m pip install -r requirements.txt` (or `requirements-dev.txt` to also get
   pytest for the test suite)

### Run manually

```bash
python scripts/fetch_cpcb.py
```

Appends to `data/raw/cpcb_ncr_YYYY-MM-DD.csv`. Logs go to `logs/fetch_YYYY-MM-DD.log`.

### Tests

```bash
python -m pytest tests/ -v
```

`tests/` covers both fetch scripts (`test_fetch_cpcb.py`, `test_fetch_firms.py`).

The CPCB tests cover the reshape/pivot logic against edge cases actually seen in the
live feed: duplicate (station, pollutant, timestamp) records CPCB occasionally emits,
stations with a null/blank name, non-numeric pollutant values or coordinates, and the
CSV append path (schema-mismatch guard, no duplicate headers). One test pins a real
bug found while hardening this: an earlier pivot implementation using
`pivot_table(dropna=False)` silently exploded 61 real NCR stations into 915 rows by
cross-joining every distinct state/city value in the dataset against every station.

The FIRMS tests cover the "200 OK with a plain-text error body" gotcha (see below),
multi-source combination/tagging, and the same CSV-append guards as CPCB.

### IMPORTANT: run from a residential connection, and the User-Agent header matters

This endpoint timed out on every request from a cloud sandbox (consistent with
datacenter IPs being blocked, same pattern as the Grid Sentinel project's data pulls),
so keep running this from a home machine.

Separately, we also found that **authenticated requests silently hang** (no response,
not even an error) unless the request carries a browser-like `User-Agent` header —
Python's default `requests`/`urllib` UA gets stuck server-side with no response at all,
while an identical request with a Chrome-style UA returns instantly. `fetch_cpcb.py`
already sets this header, but if you fork the request logic elsewhere, carry it over.

### Daily automation (Windows Task Scheduler)

`scripts/run_and_push.ps1` runs the fetch, then commits and pushes any new data to
GitHub. Already registered as scheduled task **`AQI-NIS-CPCB-Fetch`**, triggering
daily at **12:00 and 19:00** — matching Grid Sentinel's `GridSentinel-Download`
schedule. `Get-ScheduledTask -TaskName "AQI-NIS-CPCB-Fetch"` to inspect it, or open
Task Scheduler's GUI.

Manually running the wrapper directly (`powershell -File scripts\run_and_push.ps1`)
was verified end-to-end: fetch, append, commit, push all succeeded. Triggering the
*registered task* via `schtasks /run` from within this automated session reported
success but never actually launched the script (no log output, no process) — almost
certainly a window-station/session-isolation artifact of driving Task Scheduler from
an automated tool rather than a real interactive desktop session, since Grid
Sentinel's identical setup (`LogonType: Interactive`) has genuine run history on this
machine. Worth a quick manual sanity check the first time it's meant to fire (12:00 or
19:00) — right-click the task in Task Scheduler -> Run, or just check
`logs/run_and_push_YYYY-MM-DD.log` after that time passes with the machine on.

### Data schema

One row per station per hourly timestamp:

| column | meaning |
|---|---|
| `fetched_at_utc` | when this script pulled the record |
| `country`, `state`, `city`, `station` | station identity/location tag from CPCB |
| `latitude`, `longitude` | station coordinates |
| `last_update` | CPCB's reported observation timestamp |
| `PM2.5`, `PM10`, `NO2`, `SO2`, `CO`, `OZONE` | pollutant averages (units per CPCB, typically µg/m³ or mg/m³ for CO) |

## Component: NASA FIRMS fire detections

`scripts/fetch_firms.py` pulls active-fire detections over Punjab, Haryana, Delhi NCR,
and western/central Uttar Pradesh — the stubble-burning source belt, not just the 5
monitored cities — for the regional-transport side of spike attribution (component 4).
Appends one row per fire detection to `data/raw/firms_fires_YYYY-MM-DD.csv`.

### Setup

1. Get a free key at [FIRMS Map Key](https://firms.modaps.eosdis.nasa.gov/api/map_key/)
   — just an email address, arrives immediately, no approval wait.
2. Add it to `.env` as `FIRMS_MAP_KEY`.

### Run manually

```bash
python scripts/fetch_firms.py
```

### Sensor choice

Pulls both `VIIRS_NOAA20_NRT` and `VIIRS_NOAA21_NRT` (375m resolution — fine enough to
catch individual agricultural fires, unlike MODIS's 1km) for full daily coverage across
their differing overpass times. Deliberately **not** using `VIIRS_SNPP_NRT`: FIRMS
announced on 2026-09-17 that Suomi NPP product delivery ceases **2026-11-01** — squarely
inside this year's stubble-burning season (peaks Oct–Nov), so a pipeline built on it
would have broken exactly when it mattered. If you see NOAA-20/21 get deprecated later,
re-check the source list at the [Area API docs](https://firms.modaps.eosdis.nasa.gov/api/area/)
before swapping sensors.

### A real gotcha this API has

FIRMS returns **HTTP 200 with a plain-text error message** (not CSV) for a bad key,
an exceeded quota, or a malformed request — `raise_for_status()` doesn't catch this.
`fetch_firms.py` detects it by checking whether the response body actually parses as
CSV before handing it to pandas.

### Data schema

One row per fire detection:

| column | meaning |
|---|---|
| `fetched_at_utc` | when this script pulled the record |
| `source` | which satellite/sensor combo (`VIIRS_NOAA20_NRT` / `VIIRS_NOAA21_NRT`) |
| `latitude`, `longitude` | detection location |
| `acq_date`, `acq_time` | satellite acquisition date/time (UTC) |
| `confidence` | FIRMS confidence flag (nominal/low/high) |
| `frp` | fire radiative power (MW) — rough intensity proxy |
| `bright_ti4`, `bright_ti5`, `scan`, `track`, `satellite`, `instrument`, `version`, `daynight` | passed through as-is from FIRMS |

## Reachability check (2026-09-17, from the same machine this pipeline runs on)

| Source | Status |
|---|---|
| `earthengine.googleapis.com` | Reachable (fast response) |
| `cds.climate.copernicus.eu` | Reachable (fast response) |
| `firms.modaps.eosdis.nasa.gov` | Reachable (fast response) |

None of these showed the CPCB endpoint's blocking/hanging behavior. Worth re-checking
once real auth'd calls are wired up, since a plain TCP/TLS reachability check doesn't
rule out an API-layer restriction the way CPCB's User-Agent gate did.

## Not yet built

- Satellite AOD pull (Google Earth Engine).
- ERA5 wind/met data (Copernicus CDS).
- Drift detection, gap-filling, neighbor-consistency, and stubble-burning attribution
  models.
- Dashboard.
