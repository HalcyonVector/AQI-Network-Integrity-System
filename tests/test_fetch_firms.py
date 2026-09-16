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


def test_append_to_csv_adds_fetched_at_column(tmp_path, logger, monkeypatch):
    monkeypatch.setattr(fetch_firms, "RAW_DATA_DIR", tmp_path)
    df = pd.DataFrame({"latitude": [30.1], "longitude": [75.2]})
    out_path = fetch_firms.append_to_csv(df, logger)
    written = pd.read_csv(out_path)
    assert "fetched_at_utc" in written.columns
    assert written.iloc[0]["latitude"] == 30.1


def test_append_to_csv_rejects_schema_mismatch(tmp_path, logger, monkeypatch):
    monkeypatch.setattr(fetch_firms, "RAW_DATA_DIR", tmp_path)
    df1 = pd.DataFrame({"latitude": [30.1], "longitude": [75.2]})
    df2 = pd.DataFrame({"latitude": [30.1], "frp": [12.0]})

    fetch_firms.append_to_csv(df1, logger)
    with pytest.raises(ValueError, match="Column mismatch"):
        fetch_firms.append_to_csv(df2, logger)


def test_append_to_csv_appends_without_duplicating_header(tmp_path, logger, monkeypatch):
    monkeypatch.setattr(fetch_firms, "RAW_DATA_DIR", tmp_path)
    df = pd.DataFrame({"latitude": [30.1], "longitude": [75.2]})

    out_path = fetch_firms.append_to_csv(df, logger)
    fetch_firms.append_to_csv(df, logger)

    lines = out_path.read_text().strip().splitlines()
    assert lines[0] == "fetched_at_utc,latitude,longitude"
    assert len(lines) == 3
