"""
CPCB National AQI sub-index tables, in both directions.

The data.gov.in feed's pollutant columns are NOT concentrations. scripts/validate_feed_units.py shows,
across ~800 readings per pollutant, that each is the CPCB AQI SUB-INDEX of the standard averaging window:

    PM2.5, PM10, NO2, SO2   24-hour mean
    OZONE, CO               current 8-hour mean

(NO2 and SO2 fit with NO unit conversion: OpenAQ labels them "ppb" but the numbers are CPCB's native ug/m3,
as CO's are mg/m3.) Because the tables are monotone, a sub-index can be INVERTED back to a concentration --
a daily-scale average, never an hourly value. Indices at or above the top knot (500) are censored: the true
concentration is only known to be at least that high.
"""

from __future__ import annotations

import numpy as np

# (concentration, sub-index) knots, piecewise-linear between them.
# PM, NO2, SO2: ug/m3 (24 h). Ozone: ug/m3 (8 h). CO: mg/m3 (8 h).
BREAKPOINTS = {
    "PM2.5": [(0, 0), (30, 50), (60, 100), (90, 200), (120, 300), (250, 400), (500, 500)],
    "PM10": [(0, 0), (50, 50), (100, 100), (250, 200), (350, 300), (430, 400), (800, 500)],
    "NO2": [(0, 0), (40, 50), (80, 100), (180, 200), (280, 300), (400, 400), (800, 500)],
    "SO2": [(0, 0), (40, 50), (80, 100), (380, 200), (800, 300), (1600, 400), (2400, 500)],
    "OZONE": [(0, 0), (50, 50), (100, 100), (168, 200), (208, 300), (748, 400), (1000, 500)],
    "CO": [(0, 0), (1, 50), (2, 100), (10, 200), (17, 300), (34, 400), (50, 500)],
}
# feed column -> the trailing statistic the sub-index is computed from (established by validation)
STATISTIC = {"PM2.5": "mean24", "PM10": "mean24", "NO2": "mean24", "SO2": "mean24", "OZONE": "mean8", "CO": "mean8"}
UNITS = {"PM2.5": "ug/m3", "PM10": "ug/m3", "NO2": "ug/m3", "SO2": "ug/m3", "OZONE": "ug/m3", "CO": "mg/m3"}
TOP_INDEX = 500.0


def sub_index(pollutant: str, conc):
    """Concentration -> CPCB sub-index (scalar or array), clamped to [0, 500]."""
    xs, ys = zip(*BREAKPOINTS[pollutant])
    return np.interp(conc, xs, ys)


def concentration_from_subindex(pollutant: str, index):
    """Sub-index -> concentration (scalar or array). Inverse of sub_index on [0, 500]; values above 500 are
    clamped to the top knot (see is_censored)."""
    xs, ys = zip(*BREAKPOINTS[pollutant])
    return np.interp(index, ys, xs)


def is_censored(index):
    """True where the sub-index sits at the table's ceiling, so the concentration is only a lower bound."""
    return np.asarray(index) >= TOP_INDEX
