# NCR AQI Network Integrity System

Layered system over Delhi NCR's CAAQMS network (Delhi, Noida, Gurugram, Ghaziabad,
Faridabad) that will eventually:

1. Flag stations whose reported PM2.5 drifts from a satellite-AOD-based independent estimate.
2. Fill gaps in reported data using that same satellite-based estimate.
3. Cross-check stations against wind-conditioned neighbor consistency to catch faults
   during nighttime/cloud cover when satellite can't see.
4. Attribute PM2.5 spikes to regional stubble-burning transport (NASA FIRMS fire data +
   wind vectors) versus local sources.

Only step 0 — the CPCB data pipeline — is built so far. Satellite AOD (Google Earth
Engine), ERA5 wind/met (Copernicus CDS), NASA FIRMS, and the modeling/dashboard layers
are not started.

## Current component: CPCB fetch script

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

Covers the reshape/pivot logic against edge cases actually seen in the live feed:
duplicate (station, pollutant, timestamp) records CPCB occasionally emits, stations
with a null/blank name, non-numeric pollutant values or coordinates, and the CSV
append path (schema-mismatch guard, no duplicate headers). One test pins a real bug
found while hardening this: an earlier pivot implementation using
`pivot_table(dropna=False)` silently exploded 61 real NCR stations into 915 rows by
cross-joining every distinct state/city value in the dataset against every station.

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
GitHub. Wire it up once:

1. Open Task Scheduler -> Create Task.
2. Trigger: Daily, at a time your machine is normally on and connected.
3. Action: Start a program
   - Program/script: `powershell.exe`
   - Arguments: `-NoProfile -ExecutionPolicy Bypass -File "D:\Projects\AQI NIS\scripts\run_and_push.ps1"`
   - Start in: `D:\Projects\AQI NIS`
4. Make sure this repo has a configured `origin` remote and you can push without an
   interactive credential prompt (stored Git credential or SSH key), since Task
   Scheduler runs unattended.

## Data schema

One row per station per hourly timestamp:

| column | meaning |
|---|---|
| `fetched_at_utc` | when this script pulled the record |
| `country`, `state`, `city`, `station` | station identity/location tag from CPCB |
| `latitude`, `longitude` | station coordinates |
| `last_update` | CPCB's reported observation timestamp |
| `PM2.5`, `PM10`, `NO2`, `SO2`, `CO`, `OZONE` | pollutant averages (units per CPCB, typically µg/m³ or mg/m³ for CO) |

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
- NASA FIRMS fire data pull.
- Drift detection, gap-filling, neighbor-consistency, and stubble-burning attribution
  models.
- Dashboard.
