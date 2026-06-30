import pytest
from src.normalize import (
    normalize_phone,
    normalize_date,
    normalize_year,
    normalize_country,
    normalize_skill,
    NormResult
)

def test_normalize_phone():
    # Valid E164 inputs or parseable globals
    res = normalize_phone("+1 415 555 2671")
    assert res.normalized is True
    assert res.value == "+14155552671"
    assert res.method == "phone_e164"

    res = normalize_phone("09876543210")  # fallback to IN default parse if valid
    # In India, 09876543210 is valid
    assert res.normalized is True
    assert res.value.startswith("+91")

    # Unparseable inputs
    res = normalize_phone("not-a-phone-number")
    assert res.normalized is False
    assert res.value == "not-a-phone-number"
    assert res.method == "phone_parse_failed"

    # Empty inputs
    res = normalize_phone("   ")
    assert res.normalized is False
    assert res.value == "   "
    assert res.method == "phone_empty"

def test_normalize_date():
    # 'Month YYYY' format
    res = normalize_date("January 2021")
    assert res.normalized is True
    assert res.value == "2021-01"

    # 'Month, YYYY' format
    res = normalize_date("January, 2022")
    assert res.normalized is True
    assert res.value == "2022-01"

    # 'YYYY-MM' format
    res = normalize_date("2021-06")
    assert res.normalized is True
    assert res.value == "2021-06"

    # 'YYYY/MM' format
    res = normalize_date("2021/06")
    assert res.normalized is True
    assert res.value == "2021-06"

    # 'MM/YYYY' format
    res = normalize_date("06/2021")
    assert res.normalized is True
    assert res.value == "2021-06"

    # 'YYYY' format
    res = normalize_date("2021")
    assert res.normalized is True
    assert res.value == "2021"

    # Present variants
    for p in ["Present", "current", "now", "ongoing", ""]:
        res = normalize_date(p)
        assert res.normalized is True
        assert res.value is None

    res = normalize_date(None)
    assert res.normalized is True
    assert res.value is None

    # Invalid formats
    for bad in ["Jan 2021", "2021-13", "13/2021", "sometime in 2022"]:
        res = normalize_date(bad)
        assert res.normalized is False
        assert res.value == bad
        assert res.method == "date_unrecognized"

def test_normalize_year():
    assert normalize_year(2018).value == 2018
    assert normalize_year("2020").value == 2020
    assert normalize_year("Class of 2019").value == 2019
    assert normalize_year("2019-06").value == 2019
    
    res = normalize_year("graduated")
    assert res.normalized is False
    assert res.value is None

    assert normalize_year(None).value is None
    assert normalize_year("").value is None

def test_normalize_country():
    # Common aliases
    assert normalize_country("UK").value == "GB"
    assert normalize_country("USA").value == "US"
    assert normalize_country("England").value == "GB"
    assert normalize_country("Seoul, South Korea").value == "KR"

    # Free text
    assert normalize_country("Bangalore, India").value == "IN"
    assert normalize_country("San Francisco, CA, US").value == "US"

    # Unresolvable
    res = normalize_country("Narnia")
    assert res.normalized is False
    assert res.value is None

    # Empty
    assert normalize_country(None).value is None
    assert normalize_country("").value is None

def test_normalize_skill():
    # Exact / near match >= 87 score
    res = normalize_skill("python")
    assert res.normalized is True
    assert res.value == "Python"
    assert res.penalty == 0.0

    res = normalize_skill("pythn")
    assert res.normalized is True
    assert res.value == "Python"

    # Match below 87 — penalty is now 0.0 on the NormResult itself;
    # the normalized=False flag alone triggers the standard _NORM_FAIL_P
    # penalty downstream in merge._conf(), preventing double-counting.
    res = normalize_skill("React.js")
    assert res.penalty == 0.0
    assert res.value == "React.js"
    assert res.normalized is False

    # Empty
    res = normalize_skill("")
    assert res.normalized is False
    assert res.penalty == 0.0
