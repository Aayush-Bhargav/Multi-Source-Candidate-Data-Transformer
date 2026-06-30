import pytest
from pydantic import ValidationError
from src.schema import CandidateFragment, CanonicalProfile

def test_candidate_fragment_optional_fields():
    # CandidateFragment must allow all fields to be optional/missing
    frag = CandidateFragment()
    assert frag.source_identifier is None
    assert frag.raw_extracted_id is None
    assert frag.full_name is None
    assert frag.emails == []
    assert frag.phones == []
    assert frag.location_raw is None
    assert frag.headline is None
    assert frag.skills_raw == []
    assert frag.experience_raw == []
    assert frag.education_raw == []
    assert frag.links_raw is None

def test_canonical_profile_validation_pass():
    profile = CanonicalProfile(
        candidate_id="stable_sha_hash1",
        full_name="Jane Doe",
        emails=["jane@example.com"],
        phones=["+14155552671"],
        location={"city": "San Francisco", "region": "CA", "country": "US"},
        links={
            "linkedin": "https://linkedin.com/in/janedoe",
            "github": "https://github.com/janedoe",
            "portfolio": None,
            "other": []
        },
        headline="Senior Staff Engineer",
        years_experience=5.5,
        skills=[
            {"name": "Python", "confidence": 0.95, "sources": ["csv"]}
        ],
        experience=[
            {
                "company": "Google",
                "title": "Software Engineer",
                "start": "2020-01",
                "end": "2022-12",
                "summary": "Built core services"
            }
        ],
        education=[
            {
                "institution": "MIT",
                "degree": "BS",
                "field": "CS",
                "end_year": 2018
            }
        ],
        overall_confidence=0.85,
        provenance=[
            {
                "field": "full_name",
                "source": ["csv"],
                "method": "direct"
            }
        ],
        field_metadata={"full_name": {"confidence": 0.9, "sources": ["csv"], "method": "direct"}}
    )
    assert profile.candidate_id == "stable_sha_hash1"
    assert profile.field_metadata["full_name"]["confidence"] == 0.9

def test_canonical_profile_validation_fail_confidence():
    with pytest.raises(ValidationError) as exc_info:
        CanonicalProfile(
            candidate_id="id1",
            full_name="Name",
            overall_confidence=1.2  # Should fail > 1.0
        )
    assert "overall_confidence must be in [0.0, 1.0]" in str(exc_info.value)

    with pytest.raises(ValidationError) as exc_info:
        CanonicalProfile(
            candidate_id="id1",
            full_name="Name",
            overall_confidence=-0.1  # Should fail < 0.0
        )
    assert "overall_confidence must be in [0.0, 1.0]" in str(exc_info.value)

def test_canonical_profile_validation_fail_location_keys():
    with pytest.raises(ValidationError) as exc_info:
        CanonicalProfile(
            candidate_id="id1",
            full_name="Name",
            location={"city": "SF", "country": "US"}  # missing "region"
        )
    assert "location is missing required key(s)" in str(exc_info.value)

def test_canonical_profile_validation_fail_links_keys():
    with pytest.raises(ValidationError) as exc_info:
        CanonicalProfile(
            candidate_id="id1",
            full_name="Name",
            links={"linkedin": None, "github": None, "portfolio": None}  # missing "other"
        )
    assert "links is missing required key(s)" in str(exc_info.value)

def test_canonical_profile_validation_fail_years_exp():
    with pytest.raises(ValidationError) as exc_info:
        CanonicalProfile(
            candidate_id="id1",
            full_name="Name",
            years_experience=-1.5  # must be non-negative
        )
    assert "years_experience must be >= 0.0" in str(exc_info.value)

def test_canonical_profile_validation_fail_skills_structure():
    with pytest.raises(ValidationError) as exc_info:
        CanonicalProfile(
            candidate_id="id1",
            full_name="Name",
            skills=[{"name": "Python", "confidence": 0.8}]  # missing "sources"
        )
    assert "skills[0] is missing required key(s)" in str(exc_info.value)

def test_field_metadata_serialization_exclusion():
    profile = CanonicalProfile(
        candidate_id="id1",
        full_name="Name",
        field_metadata={"test": "data"}
    )
    dumped = profile.model_dump()
    assert "field_metadata" not in dumped
    # Make sure we can access it in memory
    assert profile.field_metadata == {"test": "data"}

def test_round_trip_safety():
    profile = CanonicalProfile(
        candidate_id="id1",
        full_name="Name",
        emails=["test@test.com"],
        location={"city": "A", "region": "B", "country": "C"},
        links={"linkedin": None, "github": None, "portfolio": None, "other": []}
    )
    dumped = profile.model_dump()
    restored = CanonicalProfile.model_validate(dumped)
    assert restored.candidate_id == profile.candidate_id
    assert restored.emails == profile.emails
    assert restored.location == profile.location
    assert restored.links == profile.links
