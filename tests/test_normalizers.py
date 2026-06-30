"""test_normalizers.py — Strict normalizer invariant tests.

Asserts that normalize_date strictly rejects non-whitelisted formats
without default-filling from the system clock.
"""
import datetime
from src.normalize import normalize_date, normalize_phone, normalize_country


def test_normalize_date_rejects_fuzzy_formats():
    """Non-whitelisted date strings must be returned verbatim with normalized=False."""
    fuzzy_inputs = [
        "Summer 2022",
        "Q1 2023",
        "Late 2021",
        "Fall 2019",
        "mid-2020",
        "Around 2018",
        "2022-Q3",
        "sometime in 2022",
    ]
    for raw in fuzzy_inputs:
        res = normalize_date(raw)
        assert res.normalized is False, (
            f"normalize_date('{raw}') should reject but got normalized=True"
        )
        assert res.value == raw, (
            f"normalize_date('{raw}') should return verbatim but got '{res.value}'"
        )
        assert res.method == "date_unrecognized"


def test_normalize_date_no_clock_dependency():
    """Dates must NEVER be filled from the current system clock.

    If normalize_date were using dateutil with defaults, 'January' alone
    would silently pick a year from the current date.  We verify that
    'January' (without a year) is rejected, not silently enriched.
    """
    res = normalize_date("January")
    assert res.normalized is False
    assert res.value == "January"


def test_normalize_date_accepts_whitelisted_formats():
    """All whitelisted formats must be accepted and normalized correctly."""
    cases = [
        ("January 2021",   "2021-01"),
        ("December, 2023", "2023-12"),
        ("2021-06",        "2021-06"),
        ("2021/06",        "2021-06"),
        ("06/2021",        "2021-06"),
        ("2021",           "2021"),
        ("Present",        None),
        ("current",        None),
        ("",               None),
        (None,             None),
    ]
    for raw, expected in cases:
        res = normalize_date(raw)
        assert res.normalized is True, (
            f"normalize_date('{raw}') should accept but got normalized=False"
        )
        assert res.value == expected, (
            f"normalize_date('{raw}') expected '{expected}' but got '{res.value}'"
        )


def test_normalize_phone_default_region():
    """The default_region parameter should be tried first in the fallback chain."""
    # A number that's valid in India but ambiguous without prefix
    res = normalize_phone("9876543210", default_region="IN")
    assert res.normalized is True
    assert res.value.startswith("+91")


def test_normalize_country_us_state_guard():
    """US state abbreviations must not collide with country codes.

    CA should resolve to US (California), not CA (Canada).
    IN should resolve to US (Indiana), not IN (India).
    """
    res = normalize_country("San Francisco, CA")
    assert res.value == "US", f"Expected 'US' for 'San Francisco, CA', got '{res.value}'"

    res = normalize_country("Indianapolis, IN")
    assert res.value == "US", f"Expected 'US' for 'Indianapolis, IN', got '{res.value}'"

    res = normalize_country("Berlin, DE")
    assert res.value == "US", f"Expected 'US' for 'Berlin, DE' (state guard), got '{res.value}'"

    # But a standalone country name should still work
    res = normalize_country("India")
    assert res.value == "IN"

    res = normalize_country("Canada")
    assert res.value == "CA"
