# Stubble replication on 2015 to 2025: protocol

Written on 2026-11-22, before the analysis was run on any past season. This file is committed first so that the
commit time shows the choices below were fixed before any result existed. The analysis code is written after it and
may not change any choice listed here.

## Question

Does the pattern found in Delhi in autumn 2026 repeat in the ten seasons before it? The pattern: on rain-free days,
Delhi's daily PM2.5 rises with the day's exposure to smoke from regional crop fires carried on the 925 hPa wind,
after controlling for boundary-layer height and temperature.

The 2026 season itself is not part of this replication. Its own pre-registered test is separate, has not been run, and
stays reserved (it needs at least 20 rain-free days with PM2.5, and must be run once). Nothing here uses it.

## Unit of analysis

One season is 1 October to 30 November of one year, 2015 to 2025: eleven seasons. Each season is analysed on its own.
Seasons are never pooled into one regression.

## Inputs

| Input | Source | Detail |
|---|---|---|
| Delhi PM2.5 | `data/processed/historical/delhi_pm25_daily.csv` | Mean over Delhi stations of the station-day mean; a station-day needs at least 18 clean hours; a day needs at least 3 stations (chosen now; the live version had no minimum because it had 20 or more stations) |
| Fires | `data/historical/firms/firms_VIIRS_SNPP_SP_{year}.csv` | Suomi NPP only. One sensor exists in every year, so exposure is comparable across seasons. Low-confidence detections are dropped, as live |
| Winds | `era5_pressure_{10,11}_2015-2025.nc` | 925 hPa u and v, 3-hourly, 73.5 to 81.0 E and 27.5 to 32.5 N. The same domain and step as the live pull |
| Controls | `era5_single_{10,11}_2015-2025.nc` | Boundary-layer height and 2 m temperature on the same grid |
| Rain | `era5_precip_{10,11}_2015-2025.nc` | Hourly, 28.0 to 29.0 N and 76.75 to 77.75 E |

## Method (unchanged from the 2026 code)

The existing functions in `scripts/analyze_stubble_transport.py` are imported and used as they are:

1. Each fire releases a parcel that is advected for up to 72 hours along the 925 hPa wind, hourly steps, linear in time
   and bilinear in space, with no tuning factor. A hit is a parcel passing within 60 km of Delhi (28.61 N, 77.21 E).
   Fires already inside that radius are excluded as local.
2. The daily exposure index is the fire radiative power of hits arriving on each IST day.
3. Daily boundary-layer height (the day's maximum) and temperature (the day's mean) are averaged over ERA5 cells within
   60 km of Delhi.
4. A day is wet if the area-mean rain is at least 1 mm, and the two days after a wet day count as washout. Rain-free days
   are the days that are not washout.
5. The analysis window of each season is the dates whose arriving parcels are fully observable: it starts 72 hours
   after the wind record starts (5 October) and ends when the day's last hour is covered (30 November).

## The test, per season

- Days: rain-free days in the window that have Delhi PM2.5.
- Statistic: Spearman partial correlation between the exposure index (925 hPa, lag 0) and Delhi daily PM2.5, controlling
  for boundary-layer height and temperature (`partial_spearman`).
- p-value: `partial_spearman` returns a two-sided permutation p (5,000 permutations, fixed seed). It is converted to a one-sided
  p in the positive direction as p / 2 when the partial correlation is positive, and 1 - p / 2 when it is negative. The
  permutation null is symmetric, so this conversion is exact up to permutation error.
- A season is testable if it has at least 20 rain-free days with PM2.5. A season with fewer is reported as not testable and
  excluded from the summary below. This is the same minimum as the 2026 protocol.

## Decision rule across seasons (fixed now)

The replication holds if both are true:
1. The one-sided p-values of the testable seasons, combined by Stouffer's method (equal weights: z = the standard normal
   quantile of 1 - p for each season, summed and divided by the square root of the number of seasons), give a combined
   one-sided p below 0.05.
2. The median partial correlation across testable seasons is positive.

Otherwise it does not hold. If it does not hold, the dashboard and README say so in plain words.

Always reported, whatever the outcome: every season's n, partial correlation and p-value, the count of seasons with a
positive sign and with p below 0.05, the number of Delhi stations behind each season, and a per-season forest plot.
Early seasons rest on 5 to 7 Delhi stations. That is shown, not hidden.

## Secondary results (reported, not used in the decision)

1. Circular-shift p-value for the same statistic, which respects day-to-day autocorrelation.
2. The same statistic on all days, not only rain-free ones (shows how much rain mattered).
3. The wind-blind baseline: the count of fires within 400 km, on the same days, same controls.
4. Exposure from Suomi NPP and NOAA-20 together, for 2018 onwards.

## Differences from the 2026 version, declared in advance

1. Fire sensor: live 2026 uses NOAA-20 and NOAA-21 near-real-time; the archive has Suomi NPP from 2015 and NOAA-20 from 2018
   in standard processing. The primary uses Suomi NPP alone; secondary 4 shows the two-sensor version.
2. Rain box: 28.0 to 29.0 N and 76.75 to 77.75 E, slightly smaller than the live 28.0 to 29.2 N and 76.6 to 77.8 E.
3. Station minimum: at least 3 Delhi stations per day (the live data always has more).
4. Station composition changes from year to year as the network grew; PM2.5 is a mean over whichever stations reported.

## Run discipline

The analysis is run once. If it fails for a technical reason, only the bug is fixed, no choice above is touched, and each
rerun is listed in the results note. No season is dropped, added or re-weighted after a result has been seen. No threshold is
changed after a result has been seen.

## What a result would and would not mean

A positive replication would say the association between smoke exposure and Delhi PM2.5 on rain-free days, net of boundary-layer
height and temperature, is not specific to 2026. It would not give a share of Delhi's PM2.5 caused by burning: the model has no
vertical mixing, deposition or plume rise, uses one receptor, and the exposure index measures smoke arrival, not mass.
