import os
import datetime
import pytest
from pathlib import Path
from src.merge import (
    merge_fragments,
    calc_years_exp,
    _group_fragments,
    _candidate_id,
    _ws,
    _conf,
    _resolve_scalar
)
from src.schema import CanonicalProfile

@pytest.fixture(autouse=True)
def clean_ledger():
    # Setup: remove conflict ledger if exists
    ledger_path = Path("data/output/conflict_ledger.log")
    if ledger_path.exists():
        ledger_path.unlink()
    yield
    # Teardown: clean up
    if ledger_path.exists():
        ledger_path.unlink()

def test_group_fragments():
    # Union-find based grouping
    f1 = {"source_identifier": "csv", "emails": ["a@example.com"], "phones": ["+14155552671"], "raw_extracted_id": "id1"}
    f2 = {"source_identifier": "ats_json", "emails": ["b@example.com"], "phones": ["+14155552671"]}  # Match phone with f1
    f3 = {"source_identifier": "github", "emails": ["a@example.com"], "phones": []}  # Match email with f1
    f4 = {"source_identifier": "resume", "emails": ["other@example.com"], "phones": ["+12065551234"]}  # Distinct

    groups = _group_fragments([f1, f2, f3, f4])
    # Should resolve to 2 groups: [f1, f2, f3] and [f4]
    assert len(groups) == 2
    group_sizes = sorted([len(g) for g in groups])
    assert group_sizes == [1, 3]

def test_candidate_id_generation():
    # ID based on lowercased email -> E.164 phone -> explicitly provided ID
    # Email exists
    g1 = [{"emails": ["JANE@example.com"], "phones": ["+14155552671"], "raw_extracted_id": "C001"}]
    # Phone exists, no email
    g2 = [{"emails": [], "phones": ["+14155552671"], "raw_extracted_id": "C001"}]
    # ID exists, no email or phone
    g3 = [{"emails": [], "phones": [], "raw_extracted_id": "C001"}]

    cid1 = _candidate_id(g1)
    cid2 = _candidate_id(g2)
    cid3 = _candidate_id(g3)

    assert len(cid1) == 16
    assert len(cid2) == 16
    assert len(cid3) == 16
    
    # Asserting stable output
    import hashlib
    assert cid1 == hashlib.sha256(b"jane@example.com").hexdigest()[:16]
    assert cid2 == hashlib.sha256(b"+14155552671").hexdigest()[:16]
    assert cid3 == hashlib.sha256(b"C001").hexdigest()[:16]

def test_candidate_id_sorting_determinism():
    # Check that regardless of fragment list order or internal dict key order,
    # the emails/phones/IDs are sorted alphabetically before choosing the first one.
    g1 = [
        {"emails": ["beta@example.com"]},
        {"emails": ["alpha@example.com"]}
    ]
    g2 = [
        {"emails": ["alpha@example.com"]},
        {"emails": ["beta@example.com"]}
    ]
    # Both should hash "alpha@example.com" because 'alpha' comes alphabetically before 'beta'
    import hashlib
    expected_hash = hashlib.sha256(b"alpha@example.com").hexdigest()[:16]
    assert _candidate_id(g1) == expected_hash
    assert _candidate_id(g2) == expected_hash

    # Test phone sorting
    gp1 = [
        {"emails": [], "phones": ["+14155559999"]},
        {"emails": [], "phones": ["+14155551111"]}
    ]
    expected_phone_hash = hashlib.sha256(b"+14155551111").hexdigest()[:16]
    assert _candidate_id(gp1) == expected_phone_hash

    # Test raw ID sorting
    gi1 = [
        {"emails": [], "phones": [], "raw_extracted_id": "xyz"},
        {"emails": [], "phones": [], "raw_extracted_id": "abc"}
    ]
    expected_id_hash = hashlib.sha256(b"abc").hexdigest()[:16]
    assert _candidate_id(gi1) == expected_id_hash

def test_confidence_scoring_formula():
    # ATS/CSV weight = 0.90, GitHub = 0.85, Resume = 0.70
    assert _ws("ats_json") == 0.90
    assert _ws("csv") == 0.90
    assert _ws("github") == 0.85
    assert _ws("resume") == 0.70

    # No penalty, no corroboration
    assert _conf(None, "ats_json") == 0.90
    assert _conf(None, "resume") == 0.70

    # Corroboration bonus (+0.10 for each additional distinct source matching the normalized value)
    assert _conf(None, "ats_json", corrob=1) == 1.0  # 0.90 + 0.10
    assert _conf(None, "resume", corrob=2) == 0.90  # 0.70 + 0.20

def test_scalar_conflict_resolution():
    # When sources conflict on scalar values (e.g. location, headline), highest Ws wins,
    # and confidence of the winning field is capped at exactly 0.40.
    # Conflicts are logged to data/output/conflict_ledger.log.

    # Candidate list: (value, source, NormResult)
    # Different locations: "New York" from ATS (0.90), "San Francisco" from Resume (0.70)
    cands = [
        ("New York", "ats_json", None),
        ("San Francisco", "resume", None)
    ]
    val, conf, sources, method = _resolve_scalar("location", cands)
    
    assert val == "New York"
    assert conf == 0.40  # capped
    assert sources == ["ats_json"]
    assert method == "conflict_resolved"

    # Verify conflict ledger log
    ledger_path = Path("data/output/conflict_ledger.log")
    assert ledger_path.exists()
    content = ledger_path.read_text()
    assert "CONFLICT field=location dropped='San Francisco'" in content

def test_experience_deduplication():
    # 1. Unstructured block handling
    from src.merge import _norm_exp
    raw_exp = {"raw_lines": ["Senior Dev", "Apple Inc.", "2020-2022"]}
    normed = _norm_exp(raw_exp, "resume")
    assert normed["company"] == "Unspecified"
    assert normed["title"] == "Unspecified"
    assert normed["summary"] == "Senior Dev | Apple Inc. | 2020-2022"

    # 2. Chronological violation: end before start -> end to None, -0.15 penalty
    chrono_violation = {
        "company": "Apple",
        "title": "Engineer",
        "start": "2022-01",
        "end": "2021-01"
    }
    normed_violation = _norm_exp(chrono_violation, "csv")
    assert normed_violation["end"] is None
    # confidence: 0.90 (csv) - 0.15 (chrono penalty) = 0.75
    assert normed_violation["_conf"] == 0.75

    # 3. Deduplication: same standardized company and title -> keep higher confidence if overlapping
    from src.merge import _dedup_exp
    as_of = datetime.date(2022, 6, 30)
    e1 = {"company": "Google LLC", "title": "Staff Engineer", "start": "2020-01", "end": "2022-01", "_src": "ats_json", "_conf": 0.90}
    e2 = {"company": "Google", "title": "Staff Engineer", "start": "2020-01", "end": "2022-01", "_src": "resume", "_conf": 0.70}
    
    deduped = _dedup_exp([e1, e2], as_of)
    assert len(deduped) == 1
    assert deduped[0]["_conf"] == 0.90

    # 4. "Unspecified" entries should not be deduplicated/collapsed
    e_unspec1 = {"company": "Unspecified", "title": "Unspecified", "start": "2020-01", "end": "2021-01", "_conf": 0.7}
    e_unspec2 = {"company": "Unspecified", "title": "Unspecified", "start": "2020-06", "end": "2021-06", "_conf": 0.7}
    deduped_unspec = _dedup_exp([e_unspec1, e_unspec2], as_of)
    assert len(deduped_unspec) == 2

def test_boomerang_employee():
    from src.merge import _dedup_exp
    as_of = datetime.date(2025, 1, 1)

    # Candidate was SWE at Google 2018-2020, left, and returned as SWE at Google 2023-2024
    e1 = {"company": "Google", "title": "Software Engineer", "start": "2018-01", "end": "2020-12", "_conf": 0.90}
    e2 = {"company": "Google", "title": "Software Engineer", "start": "2023-01", "end": "2024-12", "_conf": 0.85}

    deduped = _dedup_exp([e1, e2], as_of)
    # Since their date ranges do NOT overlap, both must be retained!
    assert len(deduped) == 2

    # If they DID overlap (e.g. 2018-2021 vs 2020-2022), they must be deduplicated
    e3 = {"company": "Google", "title": "Software Engineer", "start": "2018-01", "end": "2021-12", "_conf": 0.90}
    e4 = {"company": "Google", "title": "Software Engineer", "start": "2020-01", "end": "2022-12", "_conf": 0.85}
    deduped_overlap = _dedup_exp([e3, e4], as_of)
    assert len(deduped_overlap) == 1
    assert deduped_overlap[0]["start"] == "2018-01"

def test_calc_years_exp():
    # Calculate union of non-overlapping durations
    exp = [
        {"start": "2020-01", "end": "2020-12"}, # 12 months
        {"start": "2020-06", "end": "2021-06"}, # overlap, union is 2020-01 to 2021-06 (18 months)
        {"start": "2022-01", "end": None}        # 'Present' -> anchored to as_of
    ]
    
    as_of = datetime.date(2022, 6, 30) # 2022-01 to 2022-06 is 6 months
    # Total months = 18 + 6 = 24 months = 2.0 years
    years = calc_years_exp(exp, as_of=as_of)
    assert years == 2.0

def test_end_to_end_merge():
    # Verify overall_confidence, provenance, and final CanonicalProfile assembly
    f1 = {
        "source_identifier": "csv",
        "raw_extracted_id": "ID_123",
        "full_name": "Alice Smith",
        "emails": ["alice@gmail.com"],
        "phones": ["+14155551111"],
        "location_raw": "SF, CA, USA",
        "headline": "Lead SWE",
        "skills_raw": ["Python", "Docker"],
        "experience_raw": [
            {"company": "Google", "title": "SWE", "start": "2019-01", "end": "2020-01"}
        ]
    }
    f2 = {
        "source_identifier": "github",
        "raw_extracted_id": "gh_alice",
        "full_name": "Alice Smith",
        "emails": ["alice@gmail.com"],
        "location_raw": "San Francisco, CA",
        "headline": "SWE",
        "skills_raw": ["Go", "Docker"]
    }

    profiles, failed_count = merge_fragments([f1, f2])
    assert failed_count == 0
    assert len(profiles) == 1
    p = profiles[0]

    assert isinstance(p, CanonicalProfile)
    assert p.candidate_id == _candidate_id([f1, f2])
    assert p.full_name == "Alice Smith"
    # location_raw conflict: "SF, CA, USA" (csv, 0.90) vs "San Francisco, CA" (github, 0.85)
    # "SF, CA, USA" wins, parsed to city="SF", region="CA", country="US"
    assert p.location["city"] == "SF"
    assert p.location["country"] == "US"
    
    # overall_confidence is unweighted mean of all populated fields' confidences
    assert p.overall_confidence > 0.0
    assert len(p.provenance) > 0
    assert p.field_metadata is not None
    # Ensure field_metadata is excluded from serialization but is present in-memory
    assert "field_metadata" not in p.model_dump()


def test_overall_confidence_absent_fields():
    # Only names, emails and experience provided. Other fields omitted/null.
    # overall_confidence should be mean of only those 3 field confidences.
    f1 = {
        "source_identifier": "csv",
        "full_name": "High Confidence Name",
        "emails": ["high@example.com"],
        "experience_raw": [{"company": "A", "title": "B"}],
    }
    profiles, _ = merge_fragments([f1])
    assert len(profiles) == 1
    p = profiles[0]
    # Check that overall confidence doesn't drop due to 0.0s from other fields
    assert p.overall_confidence > 0.8  # csv base is 0.9

def test_resumes_identical_filenames():
    # If two resumes have the same filename (raw_extracted_id) but id_is_synthetic=True,
    # and NO overlapping emails/phones, they should NOT merge.
    f1 = {
        "source_identifier": "resume",
        "raw_extracted_id": "resume_123",
        "id_is_synthetic": True,
        "emails": ["alpha@example.com"]
    }
    f2 = {
        "source_identifier": "resume",
        "raw_extracted_id": "resume_123",
        "id_is_synthetic": True,
        "emails": ["beta@example.com"]
    }
    groups = _group_fragments([f1, f2])
    assert len(groups) == 2
