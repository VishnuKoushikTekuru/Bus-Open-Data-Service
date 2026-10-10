"""Tests for the BODS pipeline, run against the small sample in data/sample.

The Route 78 weekday counts were checked by hand against Arriva's published
timetable (27 outbound and 27 inbound), so they act as a regression test.
"""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import bods_pipeline as bp  # noqa: E402

SAMPLE = ROOT / "data" / "sample"


@pytest.fixture(scope="module")
def clean():
    return bp.clean_dataset(bp.build_master_dataset(SAMPLE))


def test_route_name_comes_from_linename_not_filename(clean):
    # The files are named "K78" but the route shown to passengers is "78".
    assert "78" in set(clean["route_line"])
    assert not any(r.startswith("K") for r in clean["route_line"].unique())


def test_route_78_weekday_journeys_match_published_timetable(clean):
    r = clean[(clean["route_line"] == "78") & (clean["day_type"] == "Mon-Fri")]
    counts = r.groupby("direction").size().to_dict()
    assert counts == {"inbound": 27, "outbound": 27}


def test_no_duplicate_journeys(clean):
    assert not clean.duplicated(
        subset=["route_line", "direction", "departure_time", "operating_days"]).any()


def test_every_departure_time_was_parsed(clean):
    assert clean["hour"].notna().all()


def test_shorthand_days_are_expanded():
    assert bp.label_day_type("Monday,Tuesday,Wednesday,Thursday,Friday") == "Mon-Fri"
    assert bp.label_day_type("Saturday") == "Saturday"


def test_malformed_file_returns_empty_list(tmp_path):
    bad = tmp_path / "bad.xml"
    bad.write_text("<not valid xml")
    assert bp.extract_journeys_from_file(bad) == []
