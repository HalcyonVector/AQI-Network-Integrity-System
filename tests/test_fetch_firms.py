import logging
import sys
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import fetch_firms  # noqa: E402


@pytest.fixture
def logger():
    log = logging.getLogger("test_fetch_firms")
    log.addHandler(logging.NullHandler())
    return log


class _FakeResponse:
    def __init__(self, text):
        self.text = text

    def raise_for_status(self):
        pass


def test_fetch_one_source_raises_on_plain_text_error_body(logger, monkeypatch):
    # FIRMS returns HTTP 200 with a plain-text message (not CSV) for bad keys or
    # exceeded quotas -- raise_for_status() doesn't catch this, so it has to be
    # detected by inspecting the body.
    session = fetch_firms.requests.Session()
    monkeypatch.setattr(session, "get", lambda url, timeout: _FakeResponse("Invalid MAP_KEY."))
    with pytest.raises(ValueError, match="did not return CSV"):
        fetch_firms.fetch_one_source("bad-key", "VIIRS_NOAA20_NRT", session, logger)


def test_fetch_one_source_parses_valid_csv_and_tags_source(logger, monkeypatch):
    csv_body = "latitude,longitude,acq_date,acq_time,confidence,frp\n30.1,75.2,2026-09-17,0530,80,12.5\n"
    session = fetch_firms.requests.Session()
    monkeypatch.setattr(session, "get", lambda url, timeout: _FakeResponse(csv_body))
    df = fetch_firms.fetch_one_source("good-key", "VIIRS_NOAA20_NRT", session, logger)
    assert len(df) == 1
    assert df.iloc[0]["latitude"] == 30.1
    assert df.iloc[0]["source"] == "VIIRS_NOAA20_NRT"


def test_fetch_one_source_raises_on_missing_expected_columns(logger, monkeypatch):
    csv_body = "foo,bar\n1,2\n"
    session = fetch_firms.requests.Session()
    monkeypatch.setattr(session, "get", lambda url, timeout: _FakeResponse(csv_body))
    with pytest.raises(ValueError, match="missing columns"):
        fetch_firms.fetch_one_source("good-key", "VIIRS_NOAA20_NRT", session, logger)


def test_fetch_fires_combines_multiple_sources(logger, monkeypatch):
    csv_body = "latitude,longitude,acq_date,acq_time,confidence,frp\n30.1,75.2,2026-09-17,0530,80,12.5\n"
    monkeypatch.setattr(fetch_firms.requests.Session, "get", lambda self, url, timeout: _FakeResponse(csv_body))
    df = fetch_firms.fetch_fires("good-key", logger)
    assert len(df) == len(fetch_firms.SOURCES)
    assert set(df["source"]) == set(fetch_firms.SOURCES)


def test_fetch_fires_returns_empty_df_when_no_detections(logger, monkeypatch):
    csv_body = "latitude,longitude,acq_date,acq_time,confidence,frp\n"
    monkeypatch.setattr(fetch_firms.requests.Session, "get", lambda self, url, timeout: _FakeResponse(csv_body))
    df = fetch_firms.fetch_fires("good-key", logger)
    assert df.empty


def _det(source="VIIRS_NOAA20_NRT", lat=30.1, lon=75.2, date="2026-10-05", time="0835", frp=5.0):
    return {"source": source, "latitude": lat, "longitude": lon, "acq_date": date, "acq_time": time, "confidence": "n", "frp": frp}


def test_build_url_with_and_without_start_date():
    base = fetch_firms.build_url("KEY", "VIIRS_NOAA20_NRT", 5)
    assert base.endswith("/KEY/VIIRS_NOAA20_NRT/73.5,27.5,81.0,32.5/5")
    assert fetch_firms.build_url("KEY", "VIIRS_NOAA20_NRT", 5, "2026-10-01").endswith("/5/2026-10-01")


def test_backfill_chunks_respect_the_five_day_api_limit():
    assert fetch_firms.backfill_chunks("2026-09-16", "2026-09-20") == [("2026-09-16", 5)]
    assert fetch_firms.backfill_chunks("2026-09-16", "2026-09-22") == [("2026-09-16", 5), ("2026-09-21", 2)]
    assert fetch_firms.backfill_chunks("2026-10-05", "2026-10-05") == [("2026-10-05", 1)]
    chunks = fetch_firms.backfill_chunks("2026-09-16", "2026-10-15")
    assert sum(d for _, d in chunks) == 30 and all(d <= 5 for _, d in chunks)


def test_backfill_chunks_rejects_reversed_range():
    with pytest.raises(ValueError):
        fetch_firms.backfill_chunks("2026-10-05", "2026-10-01")


def test_merge_detections_is_a_union_and_idempotent():
    a = pd.DataFrame([_det(lat=30.1), _det(lat=30.2)])
    b = pd.DataFrame([_det(lat=30.2), _det(lat=30.3)])  # one overlap, one new
    merged = fetch_firms.merge_detections(a, b)
    assert sorted(merged["latitude"]) == [30.1, 30.2, 30.3]
    again = fetch_firms.merge_detections(merged, b)
    assert len(again) == 3  # re-merging the same data changes nothing


def test_merge_detections_keeps_original_fetch_timestamp():
    old = pd.DataFrame([{**_det(), "fetched_at_utc": "first"}])
    new = pd.DataFrame([{**_det(), "fetched_at_utc": "second"}])
    assert fetch_firms.merge_detections(old, new).iloc[0]["fetched_at_utc"] == "first"


def test_merge_detections_distinguishes_satellites_seeing_the_same_spot():
    a = pd.DataFrame([_det(source="VIIRS_NOAA20_NRT")])
    b = pd.DataFrame([_det(source="VIIRS_NOAA21_NRT")])
    assert len(fetch_firms.merge_detections(a, b)) == 2


def test_write_acq_date_files_splits_by_date_and_never_loses_detections(tmp_path, logger, monkeypatch):
    monkeypatch.setattr(fetch_firms, "RAW_DATA_DIR", tmp_path)
    first = pd.DataFrame([_det(date="2026-10-04", lat=30.1), _det(date="2026-10-05", lat=30.2)])
    paths = fetch_firms.write_acq_date_files(first, logger)
    assert sorted(p.name for p in paths) == ["firms_acq_2026-10-04.csv", "firms_acq_2026-10-05.csv"]

    # a later, overlapping window: one repeat, one new detection on 10-05
    second = pd.DataFrame([_det(date="2026-10-05", lat=30.2), _det(date="2026-10-05", lat=30.9)])
    fetch_firms.write_acq_date_files(second, logger)
    day5 = pd.read_csv(tmp_path / "firms_acq_2026-10-05.csv", dtype={"acq_time": str})
    assert sorted(day5["latitude"]) == [30.2, 30.9]
    assert len(pd.read_csv(tmp_path / "firms_acq_2026-10-04.csv")) == 1  # untouched


def test_write_acq_date_files_preserves_leading_zero_in_acq_time(tmp_path, logger, monkeypatch):
    monkeypatch.setattr(fetch_firms, "RAW_DATA_DIR", tmp_path)
    fetch_firms.write_acq_date_files(pd.DataFrame([_det(time="835")]), logger)  # pandas drops the 0 from "0835"
    out = pd.read_csv(tmp_path / "firms_acq_2026-10-05.csv", dtype={"acq_time": str})
    assert out.iloc[0]["acq_time"] == "0835"
