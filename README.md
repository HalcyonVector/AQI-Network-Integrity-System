# NCR AQI Network Integrity System

Layered system over Delhi NCR's CAAQMS network (Delhi, Noida, Gurugram, Ghaziabad,
Faridabad) that will eventually:

1. Flag stations whose reported PM2.5 drifts from a satellite-AOD-based independent estimate.
2. Fill gaps in reported data using that same satellite-based estimate.
3. Cross-check stations against wind-conditioned neighbor consistency to catch faults
   during nighttime/cloud cover when satellite can't see.
4. Attribute PM2.5 spikes to regional stubble-burning transport (NASA FIRMS fire data +
   wind vectors) versus local sources.

All data pipelines are built: CPCB station data (data.gov.in feed, plus
OpenAQ's archive as a second source), NASA FIRMS fire detections, satellite AOD sampling,
and ERA5 wind. The modeling layers (drift detection,
gap-filling, neighbor-consistency, stubble-burning attribution) and dashboard are
not started.

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

`scripts/run_and_push.ps1` is the daily driver. Registered as scheduled task
**`AQI-NIS-CPCB-Fetch`**, triggering at **12:00 and 19:00** (matching Grid Sentinel's schedule), plus
`StartWhenAvailable` so a missed slot runs when the machine is next on. In order, each step isolated
so one failing never stops the others:

1. Fetches: CPCB feed, OpenAQ, FIRMS, AOD, ERA5 (each re-processes a trailing window and self-heals).
2. Derived: neighbour consistency, gap-filled series, satellite consistency, stubble transport, dashboard.
3. Commits `data/raw`, `data/processed`, `data/reference` and `dashboard/` and pushes.

It exits non-zero if any step failed, and names which ones in `logs/run_and_push_YYYY-MM-DD.log`.

**Verified:** the scheduler fires on its own (runs observed at the 12:00 and 19:00 marks on this
machine from 17 Sep onward, including through the CPCB outage, when it kept committing the other
sources). Triggering the task with `schtasks /run` *from an automated session* reported success but
launched nothing — a session-isolation artefact of that tool, not a problem with the real trigger.
Runs on days the machine was off are simply missing, which is why every fetcher re-processes a window.

### Data schema

One row per station per hourly timestamp:

| column | meaning |
|---|---|
| `fetched_at_utc` | when this script pulled the record |
| `country`, `state`, `city`, `station` | station identity/location tag from CPCB |
| `latitude`, `longitude` | station coordinates |
| `last_update` | CPCB's reported observation timestamp |
| `PM2.5`, `PM10`, `NO2`, `SO2`, `CO`, `OZONE` | **CPCB AQI sub-index values (0–500), NOT concentrations** — validated, see below |

> **These are not µg/m³ — validated.** An earlier README called them concentrations (an assumption); a
> one-station check suggested otherwise, and `scripts/validate_feed_units.py` now tests it properly: ~800
> feed readings per pollutant across ~60 stations (17–25 Sep, before the outage) against OpenAQ's true
> concentrations. Each column is the **CPCB AQI sub-index of the standard averaging window**:
>
> | column | what it is | match to the sub-index of OpenAQ's concentration (mean error; within ±5 points) |
> |---|---|---|
> | NO2 | 24 h mean | 0.6 pts; 99.5% |
> | SO2 | 24 h mean | 0.4 pts; 100% |
> | CO | **current** 8 h mean (not the 24 h max) | 1.9 pts; 94% |
> | PM10 | 24 h mean | 2.9 pts; 85% |
> | OZONE | current 8 h mean | 4.8 pts; 72% |
> | PM2.5 | 24 h mean | 6.7 pts; 65% |
>
> As raw concentrations they miss badly (PM2.5: 50 µg/m³ mean error; feed median 93 vs true 52). Spearman
> correlations with the sub-index are 0.97–0.998. PM2.5's larger point error is expected: its table is
> steepest (1.67 points per µg/m³), so small differences in how CPCB and OpenAQ average the same data show up.
>
> **Converting back:** the tables are monotone, so `scripts/convert_feed.py` inverts them
> (`data/processed/feed_concentrations.csv`). Recovered concentrations match OpenAQ to a **median relative
> error of 1.4–3.2%** for PM2.5, PM10, NO2, SO2 and CO; **ozone is worse (median 9.6%, 90th percentile
> 30%) — treat it with care.** What comes back is a **daily-scale average, never an hourly value**
> (24 h mean, or current 8 h mean for ozone/CO). Readings at the ceiling (index 500) are censored: only a
> lower bound. This is what makes the 13 Delhi stations that exist *only* in the feed, and any dates OpenAQ
> has not published, usable for daily-scale analysis.

## Component: NASA FIRMS fire detections

`scripts/fetch_firms.py` pulls active-fire detections over Punjab, Haryana, Delhi NCR,
and western/central Uttar Pradesh — the stubble-burning source belt, not just the 5
monitored cities — for the regional-transport side of spike attribution (component 4).
Writes one row per fire detection to `data/raw/firms_acq_YYYY-MM-DD.csv` — **one file per
acquisition date**, merged (union, de-duplicated) on every run.

### Setup

1. Get a free key at [FIRMS Map Key](https://firms.modaps.eosdis.nasa.gov/api/map_key/)
   — just an email address, arrives immediately, no approval wait.
2. Add it to `.env` as `FIRMS_MAP_KEY`.

### Run manually

```bash
python scripts/fetch_firms.py                                    # latest 5 days (the API maximum)
python scripts/fetch_firms.py --start 2026-09-16 --end 2026-10-16   # backfill a range
```

### Why per-acquisition-date files with a 5-day window
The first design fetched only the last 24 h and appended to a file named for the *fetch*
date. Any day the machine was off became a hole: when checked on 2026-10-16, **seven days
had zero detections** (17 Sep, 22 Sep, 1–2 Oct, 9–10 Oct, 13 Oct), and the backfill then
recovered 147, 117, 244, 246 and 120 detections on the peak-burning days among them. Even
"covered" days were short by 10–27 detections, since a 24 h pull misses what falls between
runs. Any analysis on the old data would have been silently and badly biased. Now each run
pulls 5 days and merges into per-date files, so gaps self-heal and re-runs only ever *add*
detections.

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

## Component: Satellite AOD (Google Earth Engine)

`scripts/fetch_aod.py` samples MODIS MAIAC Aerosol Optical Depth (`MODIS/061/MCD19A2_GRANULES`,
1km resolution) at each CPCB station's exact coordinates — the independent satellite
estimate that components 1 (drift detection) and 2 (gap-filling) compare reported
PM2.5 against. Reads station coordinates from the most recent `data/raw/cpcb_ncr_*.csv`,
so run `fetch_cpcb.py` at least once first.

### Setup (all through your own Google account — not something this script can do for you)

1. Go to the [Earth Engine registration page](https://console.cloud.google.com/earth-engine),
   create or pick a Google Cloud project, and register it for Earth Engine (noncommercial
   plan is free and fine for this volume).
2. Add `GEE_PROJECT=your-gcp-project-id` to `.env`.
3. Authenticate, **either**:

   **a) Your own Google login (what this repo currently uses).** Run once:
   ```bash
   python -c "import ee; ee.Authenticate(auth_mode='localhost')"
   ```
   Sign in and approve every screen; the token is saved locally and reused by the
   scheduler with no prompt. Use `localhost` mode, **not** `notebook` mode — notebook
   tokens expire after a week, which would break the daily run. If the sign-in listener
   crashes with `KeyError: 'code'` (a stray browser request, e.g. Chrome's side
   traffic), paste the printed URL into a different browser (Edge) instead.

   **b) A service account key**, if your org allows it: IAM & Admin -> Service
   Accounts -> create one -> Keys -> Add key -> JSON, save under `keys/`
   (gitignored), and set `GEE_SERVICE_ACCOUNT` and `GEE_SERVICE_ACCOUNT_KEY_PATH` in
   `.env`. Newer Google Cloud organizations block this by default
   (`iam.disableServiceAccountKeyCreation`: "Service account key creation is
   disabled"), which is why (a) exists.

### Run manually

```bash
python scripts/fetch_aod.py               # re-processes the trailing window (3..14 days ago)
python scripts/fetch_aod.py --date 2026-09-15      # or one specific date
```

Writes `data/raw/aod_ncr_YYYY-MM-DD.csv`, **one file per observation date, overwritten on
re-run** (so backfills and repeated runs never duplicate rows). Validated 2026-09-26 →
2026-10-08: 26–28 Sep were almost fully cloud-masked (0–2 of 62 stations valid), 29 Sep
had 54, and 30 Sep onward 61–62 of 62.

### Cloud/quality masking

Only pixels with `AOD_QA` bits 0-2 == 1 (clear, not cloudy/shadow/water) AND bits
8-11 == 0 (best quality) are kept; everything else comes back as `NaN` rather than a
misleading raw value. The mask is applied to each granule with its own QA band
*before* granules are averaged (compositing first would let a QA pixel from one
satellite pass filter an AOD value from another). Every station always gets a row.
Live check, 62 stations: 51 valid on 2026-09-20, 54 on 09-22, 32 on 09-24 (hazier
day). On a cloudy day, expect a lot of NaN rows — that's the satellite genuinely
being blind, which is exactly the condition component 3 (wind-conditioned neighbor
consistency) exists to cover instead.

Bug caught during the first live run: Earth Engine's `sampleRegions` silently
**drops** points whose pixel is masked instead of returning nulls, so the first
version returned 1 row for 62 stations. `complete_with_all_stations()` left-joins
back onto the full station list; a regression test pins this.

### Data schema

One row per station per date:

| column | meaning |
|---|---|
| `fetched_at_utc` | when this script pulled the record |
| `date` | the satellite observation date (not the CPCB reading date) |
| `station`, `city` | matches the CPCB station naming exactly, for joining |
| `aod_047`, `aod_055` | AOD at 0.47µm / 0.55µm, scaled; `NaN` if cloud/quality-masked |

## Component: ERA5 wind (Copernicus Climate Data Store)

`scripts/fetch_era5_wind.py` pulls 3-hourly ERA5 10m wind vectors (u/v components,
derived speed and "blowing-from" direction) over the same Punjab/Haryana/NCR/western-UP
belt as the FIRMS pull — the wind data component 3 (neighbor consistency) and
component 4 (stubble-burning transport attribution) both need.

### Setup (all through your own account — not something this script can do for you)

1. Register at [cds.climate.copernicus.eu](https://cds.climate.copernicus.eu), then
   get your Personal Access Token from your [profile page](https://cds.climate.copernicus.eu/profile).
2. **Manually accept the Terms of Use** on the
   [ERA5 hourly single levels dataset page](https://cds.climate.copernicus.eu/datasets/reanalysis-era5-single-levels)
   (bottom of the download form). The API silently rejects requests until you do
   this — it can't be done through the API itself.
3. Add to `.env`:
   ```
   CDSAPI_URL=https://cds.climate.copernicus.eu/api
   CDSAPI_KEY=your-uid:your-api-key
   ```
   (Confirmed live with `cdsapi` 0.7.7: the bare personal access token from your
   profile page — a 36-character UUID, no colon — works as-is; no `UID:` prefix.)

### Run manually

```bash
python scripts/fetch_era5_wind.py                    # re-processes the trailing window (5..12 days ago)
python scripts/fetch_era5_wind.py --date 2026-09-01
```

Writes `data/raw/era5_wind_YYYY-MM-DD.csv`, **one file per observation date, overwritten on
re-run**. **Requests can queue on CDS's
compute backend** — this is normal, not a bug; the client blocks and polls until
ready by default. First run may take a while.

### Why a trailing window (and what "ERA5T" means), and NetCDF instead of GRIB

ERA5 is published about 5 days behind real time as **ERA5T** (preliminary); the final
product replaces it months later. The default window starts 5 days back, where data
reliably exists, but it is *preliminary*: the `expver` column marks each row (`0005` = ERA5T,
revisable; `0001` = final). Confirmed on the first live run — a 7-day-old date came
back `0005`. Differences from final are usually small but not zero. When reading the
CSV, use `dtype={"expver": str}` or pandas will turn `0005` into the integer `5`.

Requested as NetCDF, not GRIB: GRIB parsing needs the `cfgrib` package, which in turn
needs the native ecCodes C library — confirmed by testing that it has no working
pip-installable path on Windows (`cfgrib` imports fine, then fails at runtime with
`Cannot find the ecCodes library`). NetCDF sidesteps this entirely since `netCDF4`
ships its own binaries.

### Data schema

One row per grid point per 3-hourly timestamp:

| column | meaning |
|---|---|
| `fetched_at_utc` | when this script pulled the record |
| `time` | ERA5 timestamp (UTC) |
| `latitude`, `longitude` | ERA5 grid point (not station-aligned — this is a coarser reanalysis grid, ~0.25°) |
| `u10_ms`, `v10_ms` | raw 10m wind components (m/s) |
| `expver` | `0005` = preliminary ERA5T (revisable), `0001` = final ERA5 |
| `wind_speed_ms` | derived magnitude |
| `wind_from_deg` | derived direction wind is blowing *from*, meteorological convention (0=N, 90=E) |
| `blh_m` | boundary-layer height (m): how deep the mixed layer is. The confounder the first stubble pass could not control for |
| `t2m_k` | 2 m temperature (K): a proxy for post-monsoon cooling that arrives with the burning season |
| `u925_ms`, `v925_ms` | wind at 925 hPa (~750 m): the transport-layer wind used for fire trajectories |

Precipitation is a separate file, `data/raw/era5_precip_YYYY-MM-DD.csv` (hourly, area mean and max over a ~130 km box
around Delhi, in mm; ERA5 labels each hour with its END). It is fetched best-effort and never blocks the wind data.

## Component: OpenAQ archive (CPCB station concentrations)

`scripts/fetch_openaq.py` reads CPCB station data from [OpenAQ](https://openaq.org)'s
open S3 archive. It exists because the data.gov.in API went down around 26 Sep 2026,
and because it is a *better* source in its own right: true concentrations (µg/m³)
rather than the AQI sub-index values the data.gov.in feed appears to carry (see the
warning under the CPCB data schema), at ~15-minute resolution (aggregated here to
hourly), plus the stations' own wind and humidity sensors.

### Setup
1. Get a free API key at [explore.openaq.org](https://explore.openaq.org) (account
   settings) and add `OPENAQ_API_KEY=` to `.env`. It is only used **once** to build
   the station mapping (and on `--refresh-map`); the daily files are read from the
   public S3 archive with no credentials.

### Run
```bash
python scripts/fetch_openaq.py                                   # last 10 days (idempotent)
python scripts/fetch_openaq.py --start 2026-09-26 --end 2026-10-07   # backfill a range
python scripts/fetch_openaq.py --refresh-map                     # rebuild station mapping
```

### Output
`data/raw/openaq_hourly_YYYY-MM-DD.csv` — **one file per observation date**, overwritten
on re-run (unlike the other pulls, which append per fetch date), so backfills and the
daily run's overlapping 10-day window never duplicate rows. Long format:

| column | meaning |
|---|---|
| `hour_start_ist` | start of the hour, IST (ISO 8601, +05:30) |
| `station` | our CPCB-feed station name, so it joins to `cpcb_ncr_*.csv` |
| `openaq_location_id` | OpenAQ's ID for that station |
| `parameter`, `units`, `value` | hourly mean; wind direction uses a circular mean |
| `n_obs` | 15-minute samples behind that hour (≤4) — low values mean a patchy hour |

Parameters: `pm25`, `pm10`, `no2`, `so2`, `co`, `o3`, `no`, `nox`, `relativehumidity`,
`temperature`, `wind_speed`, `wind_direction`. **No QC is applied** — single-station
hourly PM2.5 spikes of 500–670 µg/m³ appear in the data and were left in.

### Station mapping
`data/reference/openaq_station_map.csv` (committed) maps our station names to OpenAQ
location IDs: 60 of our 64 known stations. Matched by coordinates within 300 m, else by
identical name. **Unmatched on purpose:** `Pusa, Delhi - IITM`, `Pusa_IMD`,
`Aya Nagar, Delhi - IITM`, `North Campus, DU, Delhi - IITM` — the nearest active OpenAQ
location for each is a *different* station 2–3 km away, and a wrong match would silently
corrupt every downstream comparison.

### Things that bit during the first live run
- **Provider tags:** OpenAQ files most DPCC/UPPCB stations (JNU, Govindpuram, Wave City,
  …) under provider `N/A`, not `CPCB`. A `CPCB`-only filter silently dropped 10 real
  stations (50/64 matched; fixed → 60/64). Low-cost sensor networks (AirGradient,
  Clarity) are excluded so one near a station can't be matched in its place.
- **Stale duplicates:** OpenAQ keeps dead duplicate locations (e.g. DTU has an ID that
  last reported in 2018 alongside the live one), so only locations reporting in the last
  90 days are considered.
- **Encoding:** the archive's `units` column contains a raw `0xB5` byte ("µ"), so files
  are decoded as latin-1, not UTF-8.
- **The gas units in the `units` column are wrong.** OpenAQ labels NO₂, SO₂ and CO "ppb", but the numbers
  are CPCB's native units: **NO₂ and SO₂ in µg/m³, CO in mg/m³.** Found by the feed validation: the feed's
  NO₂/SO₂ sub-indices match OpenAQ's values only with *no* unit conversion (24.5 → 30.6 and 9.75 → 12.2 vs
  feed medians 31 and 12; applying ×1.88 / ×2.62 gives 58 and 32). Earlier text here that NO₂ at "23 ppb
  is plausible" was wrong for that reason. PM and ozone are labelled correctly (µg/m³). Do not trust the
  `units` string for NO₂, SO₂ or CO.

### Coverage and freshness
The archive **stalled 7–12 Oct 2026 and then back-filled those days about a week later**
(on 2026-10-16 it held data through 12 Oct, up from 7 Oct on 12 Oct). Upstream publication
here is late *and* retroactive, so the script re-processes the last 10 days every run
instead of fetching "yesterday"; days not yet published just log a warning and are picked
up on a later run. Backfilled: 2026-09-26 → 2026-10-12 (the whole data.gov.in outage up to
12 Oct). The 13 new Delhi DPCC stations that appeared in the data.gov.in feed after the
outage are **not in OpenAQ** (unmatched), so for them only the feed exists.

## Data-quality layer (`scripts/qc.py`)

Detects **stuck sensors** — hourly PM values that repeat *exactly* for ≥ 6 hours (hourly values average
four 15-minute readings, so that is not a plausible measurement) — and removes those hours before the
satellite-consistency and stubble analyses; neighbour-based analyses blank them for any station used as a
reference but still inspect them on the station itself. Real examples: JNU read exactly 1.00 µg/m³ for
8 h while its neighbours read 33; Vikas Sadan sat at 102 for 19 h while they read 52–83.

**Measured impact is small, and the README used to oversell it:** 111 of 21,303 station-hours (0.5%) are
stuck, and removing them barely changes neighbour predictions (MAE 0.3296 → 0.3299), because a *median*
over neighbours shrugs off one bad station. It matters for non-robust consumers (a mean over neighbours is
badly corrupted — tested) and so a stuck value does not sit inside a station's own daily means.

## Gap-filled hourly PM2.5 (`scripts/build_filled_series.py`)

`data/processed/pm25_hourly_filled.csv`: a cleaned hourly PM2.5 series per station where **every value
says what it is** (`source`): `observed`, `neighbour_fill` (an estimate), `stuck_replaced`,
`stuck_removed`, or `unfilled`. Observed values are never modified. Only what the backtest validated is
filled: **interior gaps of at most 24 hours**, from the neighbour median, each carrying
`expected_abs_error_pct` (~24%, from the backtest). Leading/trailing blanks (a station not yet reporting,
or offline) are not gaps and stay blank. Stuck hours longer than 24 h (Vikas Sadan's 37-hour run) are
removed rather than replaced.

First real run: 21,192 measured hours → 22,388 usable (+5.6%); 1,173 estimated; 111 stuck hours handled.
**Not covered:** a network-wide outage (nobody to estimate from). The satellite tracks the city's day-to-day
PM2.5 but not stations, so only a regional daily estimate is conceivable there, and it is not validated.

## Dashboard (`scripts/build_dashboard.py` → `dashboard/index.html`)

A self-contained static page (no server, no JavaScript, no external requests; inline SVG charts), rebuilt
by the daily run. It opens with **pipeline health** because this project's worst problems were silent
holes. Each source shows **OK** (fresh), **GAPS** (fresh but days missing behind it — an earlier version
showed the CPCB feed green beside 18 missing days, which is exactly the lie this state exists to prevent),
**STALE**, or **DOWN**, each judged against that source's own normal publication lag. Below that: Delhi
PM2.5 against fire exposure, station health, and agreement with the satellite — each next to its caveat.
Station names and reasons are HTML-escaped. Chart geometry is checked in tests (axis direction, bounds,
bar proportions) rather than by eye.

## Analysis: station vs satellite consistency (component 1, first pass)

`scripts/analyze_station_bias.py` joins ground PM2.5 (OpenAQ, averaged over the 10:00–14:00
IST satellite-overpass window, ≥3 hours required) to AOD at each station and looks for
stations that disagree with the satellite. Writes to `data/processed/`.

### What the first real data (2026-09-29 → 10-07, 9 usable days) showed

| question | result |
|---|---|
| Across stations **within a day**, do PM2.5 and AOD agree? | **No** — median Spearman ρ = **0.03** (daily values −0.07 … 0.46) |
| Does AOD track the **city's day-to-day** PM2.5? | **Yes** — ρ = **0.68** (n = 9 days) |
| Does a typical station follow its **own** AOD pixel over days? | **Yes** — median ρ = **0.62** (IQR 0.32–0.75, 51 stations) |
| Spatial variability within a day | AOD CV 0.27 vs PM2.5 CV 0.48 |

So the satellite carries signal **in time, not across the city**: PM2.5 differences between
stations are dominated by local sources a 1 km column measurement can't see, but regional
haze moves everything together from day to day and AOD follows that.

### Two detectors, one of them gated off
1. **Cross-sectional bias** (per-day robust fit, station residuals): written first, and the
   built-in gate correctly switched it **off** (needs median ρ ≥ 0.3; got 0.03). Kept
   because it will start working if/when the spatial signal improves, and because the gate
   is what stops it from flagging noise. Its residuals are *not* evidence of faults:
   Cantonment Area and IGNOU Maidan Garhi read ~5σ low every single day, but they are
   green south-Delhi sites — location, not malfunction, and the satellite can't tell.
2. **Temporal decoupling** (the one the data supports): each station's day-to-day ρ with
   its own AOD vs the network median, Fisher-z one-sided test, **Benjamini–Hochberg FDR
   control** across stations.

### Result: nothing flagged — and that is the honest answer
No station is significantly decoupled. Lowest raw p-values: Sector-51 Gurugram (ρ −0.32),
Indirapuram (−0.23), Sanjay Nagar (−0.17), Sector-62 Noida (−0.13), Vasundhara (−0.13),
IHBAS (−0.19) — but with 8–9 days each, **q = 0.23** for all of them, i.e. entirely
consistent with chance across 51 comparisons. They are labelled "watch", not flagged. The
detector needs roughly 3–4 weeks of paired days before it can say anything about one station.

### Update 2026-10-16 (13 dates with data, 556 paired station-days)
Cross-sectional ρ rose only to 0.13 (still gated off); the temporal network median is 0.60.
The *same* stations keep ranking lowest, and their q-values fell as days accrued —
Sector-51 Gurugram ρ −0.34 (11 days, q = 0.10), IHBAS ρ −0.43 (9 days, q = 0.10), Sanjay
Nagar −0.29 (q = 0.125), Indirapuram −0.24 (q = 0.126) — but none is below the 0.10 line, so
still **nothing is flagged**. Caution for whoever acts on this later: re-running the test as
data accumulates and flagging the first time q dips under 0.10 inflates false positives
(sequential testing). Fix a window (e.g. 4 weeks) and a threshold in advance instead.
The binding constraint is paired AOD days: Earth Engine's MCD19A2 currently ends 2026-10-09.

### Caveats worth keeping in mind
- A flag means "investigate", never "faulty": a station beside a road, kiln or construction
  site legitimately decouples from a regional satellite signal.
- Drift proper (a slow ramp) needs weeks of history; `trend_z_per_day` is exploratory.
- 26–28 Sep were cloud-blind, so the usable window is 29 Sep → 7 Oct (OpenAQ's archive
  ends 7 Oct; the data.gov.in feed is down). New data cannot accumulate until one resumes.
- Residuals use ground **concentrations** (OpenAQ). Do not run this on the data.gov.in feed
  columns — they appear to be AQI sub-indices (see the warning under the CPCB schema).

## Outage timeline (data.gov.in CPCB API)
| date | event |
|---|---|
| 2026-09-25 | last good pull; 62 stations |
| 2026-09-26 | first HTTP 500s from the API |
| by 2026-10-11 | `api.data.gov.in:443` refuses all connections (confirmed from India, Europe, US, Singapore via check-host.net, so not an ISP issue) |
| 2026-10-13 | back; schema unchanged, but **75–76 stations** instead of 62 (13 new DPCC stations, `Pusa_IMD` gone) |

**The 2026-09-26 → 2026-10-12 window is permanently missing from the data.gov.in feed**
(it serves only the current snapshot). OpenAQ covers that window for 60 of the original
stations. After the recovery the scheduler resumed on its own, with no intervention.

## Analysis: stubble-burning transport (component 4)

`scripts/analyze_stubble_transport.py` is a **screening-level** forward-trajectory model: each VIIRS fire
detection (low-confidence dropped) releases an air parcel advected up to 72 h on ERA5 winds; a "hit" is a
parcel passing within 60 km of Delhi. The daily stubble-exposure index is the FRP-weighted hits arriving per
IST day (fires already within 60 km, 254 of them, are local burning and excluded). It is compared with
Delhi's daily mean PM2.5 from OpenAQ concentrations.

**It does not, and cannot, say "X% of Delhi's PM2.5 is from stubble".** That needs a calibrated
dispersion/chemistry model we do not have.

### The headline: the apparent stubble signal is largely a rain effect

Three passes, each informed by the last (so they are **not independent tests** — a forking-paths caveat that
applies to everything below):

**Pass 1 (10 m winds).** Delhi PM2.5 stepped from 12–31 µg/m³ (27–29 Sep) to 52–69 µg/m³ from 30 Sep, the
day fire parcels began arriving. Rank correlation with exposure looked strong (ρ 0.74–0.85), but it was one
regime change, fragile to an arbitrary wind-speed factor (gone at ×2.0), and uncontrolled for the weather.

**Pass 2 (better weather, pre-registered).** Added the confounders: boundary-layer height and temperature
from ERA5, and replaced the arbitrary speed factor with 925 hPa winds (they average 3.6 m/s vs 2.0 m/s at
10 m, confirming 10 m understates transport). Primary test fixed *before* looking at results with these
variables: 925 hPa exposure, lag 0, controlling boundary-layer height and temperature. Result: ρ = 0.79,
**unchanged by the controls** (partial 0.787), the wind-aware index beats a wind-blind fire count (0.63), and
the circular-shift p reaches its floor (1/13 = 0.077), i.e. as strong as 13 days can show. The seasonal
explanation also failed on its own terms: on the dirty days the boundary layer got *deeper* (1,341 → 1,622 m)
and temperature *rose* (297.6 → 302 K), the opposite of cooling and stagnation.

**Pass 3 (rain) — this changes the conclusion.** Hourly ERA5 precipitation around Delhi:

| IST day | 26 Sep | 27 Sep | 28 Sep | 29 Sep | 30 Sep → 5 Oct | 6–10 Oct |
|---|---|---|---|---|---|---|
| rain (area-mean mm) | **9.1** | **3.6** | 0.9 | 0.03 | 0.00 | 0.0–0.6 |
| Delhi PM2.5 (µg/m³) | 15 | 12 | 19 | 32 | 52–63 | 52–69 |

**The clean days were the rainy days, and the "step" on 30 Sep is the first fully dry day.** Rain scrubbing
the air and then stopping explains the step with no stubble required. Restricting to rain-free days
(30 Sep → 10 Oct, n = 10), the association falls from 0.79 to **ρ = 0.61 (p = 0.066)**: still positive, but
weaker and not significant.

**Fair summary:** the headline step is mostly a rain effect. Within the dry period there is a moderate,
not-yet-significant positive association between fire exposure and PM2.5. Stubble contributing is plausible
and not established, and this data cannot give a magnitude.

### What would settle it (fixed in advance, to avoid trying variants until one works)
- **Primary test:** 925 hPa exposure, lag 0, **rain-free days only**, controlling boundary-layer height and
  temperature; secondary: exposed days only.
- **Sample size:** the circular-shift test (which respects autocorrelation) cannot go below p ≈ 1/n, so it needs
  **at least ~20 rain-free days** to have room to reach p < 0.05. Today there are 10. October–November is both
  peak burning and dry, so it should arrive within weeks.
- **Do not** re-run daily and act the first time p dips under 0.05 (sequential testing inflates false positives).

### Limits
- The control is rank-*linear* partial correlation: if exposure and PM share a *nonlinear* dependence on a
  control, residual confounding survives (pinned by a test). With ~13 days there is no room for a more
  flexible control.
- No vertical mixing, deposition or plume rise; a single regional receptor; ERA5T is preliminary; precipitation
  is an area mean over a ~130 km box, so local showers are smoothed.
- The analysis window is bounded by the wind record (parcels need up to 72 h of wind after launch, so the first
  usable day is 72 h after the record begins and the last must end inside it; enforced by `valid_exposure_days`).
- A 2 mm threshold, or a different washout length than 2 days, would change which days count as wet. These were
  fixed up front (1 mm; day plus 2 days after), not tuned.

## Analysis: neighbour consistency, station health, and gap-fill (components 3 and 2)

`scripts/analyze_neighbor_consistency.py` needs **no satellite**, so it works at night and under
cloud. Each station's hourly PM2.5 is predicted *leave-one-out* (its own reading is never an
input) from its up-to-6 nearest neighbours within 25 km, in log space, with a persistent per-site
offset removed. 61 stations, 408 hours (26 Sep → 12 Oct), every station has ≥3 neighbours.

### 1. Does conditioning on wind help? **No — it hurts.**
Predicting from only *upwind* neighbours (bearing within ±60° of the ERA5 wind-from direction,
wind ≥ 1.5 m/s; it applied in 42% of station-hours) vs. from all neighbours:

| regime | all neighbours (median) | inverse-distance mean | upwind only |
|---|---|---|---|
| all hours | 0.334 | 0.332 | 0.356 |
| hours the upwind rule applied | 0.308 | 0.307 | **0.358** |

(MAE in log units; lower is better.) Per station, upwind-only was better for only **9 of 60**,
median change −0.037 log, Wilcoxon p < 0.001 — significantly *worse*. Likely reasons (not tested):
ERA5 10 m wind at 0.25° is a crude proxy for the transport that matters over a few km, and
restricting to upwind neighbours leaves 2–3 stations instead of 6, so the median gets noisier.
**Don't use upwind conditioning here;** the plain neighbour median is as good as any variant. A
plausible physical idea that the data says no to — kept in the code and tests so it can be
re-checked when a finer wind source or denser network exists.

### 2. Station health: 3 flagged (all stuck sensors), 1 on watch (of 61)
Flatlines are the clearest and need no statistics — a stuck sensor repeating one value:

| station | stuck at | for | neighbours meanwhile |
|---|---|---|---|
| Vikas Sadan, Gurugram | 102 µg/m³ | 19 h, 11 h and 21 h (7–9 Oct) | 52–83 |
| Vikas Sadan, Gurugram | 112 µg/m³ | 8 h (4 Oct) | 30 |
| Vikas Sadan, Gurugram | 55.8 µg/m³ | 37 h (9–11 Oct) | 53 |
| JNU, Delhi | **1.00** µg/m³ | 8 h (29 Sep) | 33 |
| Sector 11, Faridabad | 53.6 µg/m³ | 7 h (12 Oct) | 22 |

**On watch:** IHBAS Dilshad Garden, whose daily residual vs its neighbours falls steadily from
+0.91 to −0.59 log (a ~4.5× change) over 17 days — p = 0.009 on its own after the autocorrelation
correction but q = 0.28 across the network, so *not confirmed*.

**An earlier version also flagged five stations as "unusually noisy" (Govindpuram, NSIT Dwarka,
Sector 30, Sector-51, Vikas Sadan). That was an artefact and is retracted.** Residual noise
correlates with how far a station's neighbours are (Spearman +0.44, p = 0.0004), and all five were
isolated stations — neighbours 9–12.5 km away on average vs a 5.4 km network median — which are
predicted worse *by construction*. Noise is now judged against what the station's isolation
predicts (robust log-log fit across stations); once adjusted, none of the five is noisy.

**Cross-check with the satellite analysis:** IHBAS is also one of the two stations right at the
threshold in the satellite-decoupling test (ρ −0.43, q ≈ 0.10). Two reference signals (satellite,
neighbours) pointing at the same station is worth a human look — though both use the same ground
PM2.5, so it is not independent confirmation of the *ground* data, and a station beside a road or
kiln would produce exactly this too. (An earlier draft of this README also claimed Sector-51
Gurugram was picked out by both methods; that rested on the retracted noise flag and no longer
holds.)

### How the flag rules got to 3 (what the first runs taught)
The first version flagged **14 of 61** — too many to be a fault list — and even after the first
round of fixes, 5 of the 8 remaining flags were still artefacts (item 5). Fixing it was a sequence
of real problems, each pinned by a test:
1. Significance ≠ importance: a −0.003 log/day trend (0.3%/day) was "significant". Now needs
   ≥ 0.02 log/day.
2. "Unusually noisy" fired on ordinary variation in a uniform network. Now also needs ≥ 1.5× the
   typical noise.
3. The trend test assumed independent days, but daily residuals are autocorrelated. Adjusted by
   effective sample size. Measured honestly: false positives on random-walk-like series fall from
   ~60% to ~35% — **better, still well above 5%**. So drift alone is a *flag* only if the total
   change is ≥ 0.5 log (~65%, a judgement call, not tuned); smaller, or merely large-but-not-FDR-
   significant (≥ ln 2, p < 0.05 alone), is a *watch*.
4. A smooth drift was being double-counted as "noisy" and as a sustained departure. Noise and
   episodes are now measured around each station's own robust trend.
5. **"Noisy" was measuring isolation, not behaviour** (see above). Found only by asking why the five
   flagged stations had anything in common. Lesson: when several flags share a trait, test whether the
   trait is the cause before believing the flags.

### 3. Gap-fill backtest (component 2, single-station gaps)
Mask a block of hours at one station, fill it, compare with the truth (MAE in µg/m³, scored only
where all three methods can produce a value; typical level 44–53 µg/m³):

| gap length | neighbours | persistence (last value) | same hour yesterday |
|---|---|---|---|
| 3 h | 13.6 | **13.2** | 22.6 |
| 6 h | **12.4** | 16.7 | 19.5 |
| 12 h | **11.0** | 18.1 | 19.1 |
| 24 h | **9.9** | 18.9 | 19.6 |

Neighbours beat persistence by 26–47% for gaps of 6 h or more and beat "yesterday" everywhere;
at 3 h, simply holding the last value is as good. Typical error is roughly 20–25% of the level.
(Error appears to fall with block length partly because longer blocks need 24 h of contiguous
data and so land in calmer, lower-PM periods.)

**What this does *not* cover:** a *network-wide* outage like 26 Sep → 12 Oct on the data.gov.in
feed, when no neighbours report. There the only independent signal is the satellite, which (see
the satellite analysis) tracks the *city's* day-to-day PM2.5 (ρ 0.68) but not stations
individually — so a regional daily estimate is feasible, a per-station fill is not.

### Limits
- 17 days of data; the real test is a longer record. All thresholds above are judgement calls and
  are named constants at the top of the script.
- Flatline hours are *detected* here but not yet *removed* from the other analyses (satellite
  consistency, stubble). One station's stuck hours slightly contaminate its daily means there.

## Reachability check (2026-09-17, from the same machine this pipeline runs on)

| Source | Status |
|---|---|
| `earthengine.googleapis.com` | Reachable (fast response) |
| `cds.climate.copernicus.eu` | Reachable (fast response) |
| `firms.modaps.eosdis.nasa.gov` | Reachable (fast response) |

None of these showed the CPCB endpoint's blocking/hanging behavior. Worth re-checking
once real auth'd calls are wired up, since a plain TCP/TLS reachability check doesn't
rule out an API-layer restriction the way CPCB's User-Agent gate did.

## Status of the four components

| # | component | state |
|---|---|---|
| 1 | Flag stations drifting from the satellite estimate | **First pass built** (see Analysis). Nothing flaggable yet — needs ~3–4 weeks of paired days. |
| 2 | Gap-fill missing station data | **Built.** Neighbour-based fill beats persistence for gaps ≥ 6 h (26–47% lower error); `pm25_hourly_filled.csv` labels every value's provenance. Network-wide outages are not covered (satellite only gives a regional daily estimate, unvalidated). |
| 3 | Neighbour consistency (for when the satellite is blind) | **Built.** Found 5 stuck-sensor episodes at 3 stations, plus 1 watch (an earlier "noisy" flag was an isolation artefact and was retracted). Wind conditioning was tested and *rejected* — it makes predictions worse. |
| 4 | Stubble-burning transport attribution | **Built, and humbled by a rain check.** The apparent step was largely rain stopping; on rain-free days ρ = 0.61 (p = 0.066, n = 10). Needs ~20+ dry days (Oct–Nov). |

Dashboard: **built** (see the Dashboard section); rebuilt by every daily run.

## Roadmap: what is left, and when to proceed

Dates are estimates that depend on upstream sources resuming; each item says what it is waiting on.

**Waiting on data to accumulate (nothing to build — just re-run, with the protocol fixed in advance)**

| what | waiting for | proceed when |
|---|---|---|
| Stubble attribution, the real test | ~20 rain-free days (10 so far; the primary test is in the section above) | around **25 Oct – 1 Nov**, once ERA5 and a PM source both reach ~19 Oct |
| Satellite-consistency / station health | ~28 paired AOD+PM days (13 so far); AOD lags ~7 days | around **mid-November** |
| Drift detection proper (a slow ramp) | 6–8 weeks of history | around **December** |

**Buildable once a dependency clears**

| what | depends on | decision rule |
|---|---|---|
| Use feed-derived concentrations (`convert_feed.py`) for Delhi daily PM when OpenAQ lacks the days | OpenAQ archive still ends 12 Oct | if still stalled by ~26 Oct, validate that the feed's 24 h value equals OpenAQ's calendar-day mean at Delhi scale on the pre-outage overlap, then use it for dates OpenAQ lacks |
| Re-check the feed's unit definition after the outage | OpenAQ reaching ≥ 1 week past 13 Oct | the feed returned with 13 new stations; run `validate_feed_units.py` again to confirm it still means "sub-index of the 24 h / 8 h average" |
| Health checks for the 13 feed-only stations | the item above | daily-scale checks only (no hourly neighbours); a weaker detector, label it so |
| Regional satellite-based fill for network-wide outages | ≥ 30 clear-sky paired days | validate leave-one-day-out before trusting any number |

**Decisions for you**
- Whether to **publish the dashboard** (it is a local file; nothing is shared). Options: leave local, GitHub Pages, or a
  claude.ai artifact.
- The **CPCB support follow-up** (about 19 Oct if no reply): try a second channel. The feed is back, so this is now just
  about getting an explanation.
- **CDS Terms of Use change on 28 Oct 2026**: the CDS logs say continued use after that date counts as accepting the
  updated terms, and to contact ECMWF before 27 Oct if you do not accept. ERA5 is the weather input for the stubble and neighbour analyses.
- Optionally regenerate your data.gov.in API key (it appeared in an earlier log screenshot; logs now redact it).

**Maintenance to expect**
- ERA5T (preliminary, `expver 0005`) is replaced by final ERA5 months later: re-pull old dates occasionally.
- Earth Engine runs on your saved login: if the dashboard shows AOD **STALE**, re-run `ee.Authenticate(auth_mode='localhost')`.
- FIRMS: Suomi NPP stops 1 Nov (already excluded); re-check the sensor list after that.
- Four stations (Pusa ×2, Aya Nagar, North Campus) and the 13 new ones have no OpenAQ match; revisit if OpenAQ adds them.
