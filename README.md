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
| `PM2.5`, `PM10`, `NO2`, `SO2`, `CO`, `OZONE` | **Probably CPCB AQI sub-index values (0–500 scale), NOT concentrations — see warning below.** |

> **Warning: these are likely not µg/m³.** An earlier version of this README called
> them concentrations; that was an assumption, and a cross-check suggests it is wrong.
> Comparing DTU Delhi on 2026-09-25 against OpenAQ's archive (true concentrations),
> converting OpenAQ's trailing **24-hour average** PM2.5 into the CPCB sub-index gave
> 53 at 12:00 (feed: 53) and 37 at 18:00 (feed: 39); PM10 gave 82/70 (feed: 77/67).
> That is a close match, and the feed's CO values (e.g. 51–95) are far too large to be
> concentrations. Caveat: one station, two timestamps, particulates only — CO/NO2/SO2/O3
> are unverified. **Do not compare these columns directly against AOD-derived PM2.5
> (a µg/m³ concentration) until this is settled**; invert the CPCB breakpoint tables to
> get back to concentrations, or use a source that reports concentrations (OpenAQ).

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
- **CO units are mislabelled:** the archive says `ppb`, but the median CO value is 0.8,
  which is a realistic ppm or mg/m³ figure, not 0.8 ppb. Don't trust the CO `units`
  column; the other gases check out (NO₂ median 23 ppb, plausible).

### Coverage and freshness
The archive **stalled 7–12 Oct 2026 and then back-filled those days about a week later**
(on 2026-10-16 it held data through 12 Oct, up from 7 Oct on 12 Oct). Upstream publication
here is late *and* retroactive, so the script re-processes the last 10 days every run
instead of fetching "yesterday"; days not yet published just log a warning and are picked
up on a later run. Backfilled: 2026-09-26 → 2026-10-12 (the whole data.gov.in outage up to
12 Oct). The 13 new Delhi DPCC stations that appeared in the data.gov.in feed after the
outage are **not in OpenAQ** (unmatched), so for them only the feed exists.

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

## Analysis: stubble-burning transport (component 4, first pass)

`scripts/analyze_stubble_transport.py` is a **screening-level** forward-trajectory model: each
VIIRS fire detection (low-confidence dropped) releases an air parcel advected up to 72 h on
ERA5 10 m winds; a "hit" is a parcel passing within 60 km of Delhi. The daily stubble-exposure
index is the FRP-weighted hits arriving per IST day (fires already within 60 km — 254 of
them — are local burning and excluded). It is compared with Delhi's daily mean PM2.5 from
OpenAQ concentrations. Because 10 m winds understate transport-layer winds, results are
shown for wind speed factors 1.0 / 1.5 / 2.0.

**It does not, and cannot, say "X% of Delhi's PM2.5 is from stubble".** That needs a
calibrated dispersion/chemistry model and boundary-layer data we don't have.

### What the first real period showed (27 Sep → 10 Oct, 13 days with Delhi PM2.5)
- Delhi PM2.5 **stepped** from 12–31 µg/m³ (27–29 Sep) to 52–69 µg/m³ from 30 Sep onwards. The
  first regional-fire parcels arrived on exactly that day (11 hits on 30 Sep, zero on the
  three days before), and arrivals continued every day after.
- Rank correlation with the exposure index is high (ρ = 0.85 at k=1.0, 0.74 at k=1.5; a plain
  count of fires within 400 km, ignoring wind, gets 0.63), with iid-permutation p = 0.0006 / 0.006.

### Why that is *not* a result you should lean on
1. **It is one regime change.** Thirteen days contain essentially one step (clean → dirty). A
   step in any seasonal driver would correlate this well.
2. **Season onset is a confounder we cannot remove.** Post-monsoon cooling, shallower boundary
   layers and inversions arrive over the same days as burning. We control for Delhi's
   station-measured wind speed, but it sits at 0.7–0.9 m/s nearly every day, so that control
   absorbs almost nothing. The variable that matters (boundary-layer height) is not in our data.
3. **Fragile to a free parameter.** At wind factor 2.0 the association is gone (ρ = 0.36,
   p = 0.21). Picking the best of k = 1.0/1.5/2.0 after the fact would be p-hacking; all three
   are reported. (18 tests were run across the table with no multiplicity correction.)
4. **The iid permutation test overstates significance** because both series are autocorrelated
   and dominated by the step. A circular-shift test (keeps each series' autocorrelation) gives
   p ≥ 0.077 everywhere — and 0.077 = 1/13 is the *minimum attainable* with 13 days, so the test
   cannot reach significance at this sample size, whatever the truth.
5. **Within exposed days only** (removing the clean-vs-dirty step), the *amount* of exposure
   tracks PM2.5 at k=1.0 (ρ = 0.69, n = 10), weakly at k=1.5 (0.43), and not at all at k=2.0
   (−0.39). Presence of arriving fires is suggestive; magnitude is not established.

**Fair summary:** consistent with stubble transport contributing to the step, but not
distinguishable from seasonal meteorology with this data. It becomes testable once Oct–Nov
(peak burning, when conditions vary day to day) accumulates: then the step stops dominating
and the circular-shift test has room to reach p < 0.05. Re-run `analyze_stubble_transport.py`
as data arrives, and fix the lag, wind factor and window *in advance* rather than trying
several until one works.

Other limits: no vertical mixing, deposition or plume rise; single regional receptor; ERA5T is
preliminary; the analysis window is bounded by the wind record (parcels need up to 72 h of wind
after launch, so the first usable day is 72 h after the record begins and the last must end
inside it — enforced by `valid_exposure_days`).

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
| 2 | Gap-fill missing station data from satellite | Not started. The city-level day-to-day link (ρ 0.68) suggests it is feasible at *regional* scale only; per-station gap-filling would inherit the weak spatial signal. |
| 3 | Wind-conditioned neighbour consistency (for when the satellite is blind) | Not started. Inputs exist: ERA5 wind, plus the stations' own wind/humidity sensors in the OpenAQ data. Matters more now: 26–28 Sep were cloud-blind. |
| 4 | Stubble-burning transport attribution | **First pass built** (see Analysis). Suggestive, not conclusive: one regime change in 13 days, confounded by season-onset meteorology. Needs Oct–Nov data. |

Dashboard: not started.
