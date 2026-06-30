"""
test_projector.py — Tests for the ConfigurableProjector and Eightfold directives.
"""

import pytest
from pydantic import ValidationError

from src.project import ConfigurableProjector, MissingFieldError, _resolve_path, _SENTINEL
from src.schema import CanonicalProfile


def test_resolve_path():
    data = {
        "candidate_id": "C001",
        "location": {"city": "New York", "country": "US"},
        "emails": ["alice@example.com", "alice.work@example.com"],
        "experience": [
            {"company": "Google", "title": "SWE"},
            {"company": "Apple", "title": "Senior SWE"}
        ],
        "skills": [
            {"name": "Python"},
            {"name": "React"},
            {"name": "Docker"}
        ]
    }

    # Scalars and dicts
    assert _resolve_path(data, "candidate_id") == "C001"
    assert _resolve_path(data, "location.city") == "New York"
    
    # Array indices
    assert _resolve_path(data, "emails[0]") == "alice@example.com"
    assert _resolve_path(data, "emails[-1]") == "alice.work@example.com"
    
    # Nested in array
    assert _resolve_path(data, "experience[1].company") == "Apple"
    
    # Pluck from slice
    assert _resolve_path(data, "skills[:2].name") == ["Python", "React"]

    # Missing fields
    assert _resolve_path(data, "missing_key") is _SENTINEL
    assert _resolve_path(data, "location.missing_key") is _SENTINEL
    assert _resolve_path(data, "emails[5]") is _SENTINEL


def test_projection_success():
    profile = CanonicalProfile(
        candidate_id="abc",
        full_name="Jane Doe",
        emails=["jane@example.com"],
        phones=["+14155552671"],
        location={"city": "SF", "region": "CA", "country": "US"},
        links={"linkedin": None, "github": None, "portfolio": None, "other": []},
        headline="Developer",
        years_experience=5.0,
        skills=[{"name": "Python", "confidence": 0.9, "sources": ["csv"]}],
        experience=[],
        education=[],
        overall_confidence=0.9,
        provenance=[],
        field_metadata={
            "full_name": {"confidence": 0.9, "sources": ["csv"], "method": "exact"}
        }
    )

    config = {
        "fields": [
            {"target": "name", "from": "full_name", "type": "str", "required": True},
            {"target": "primary_phone", "from": "phones[0]", "type": "str", "normalize": "e164"},
            {"target": "missing_opt", "from": "does_not_exist", "on_missing": "null"}
        ],
        "include_confidence": True
    }

    projector = ConfigurableProjector(config)
    result = projector.project(profile)

    assert result["name"] == "Jane Doe"
    # Normalization applied
    assert result["primary_phone"] == "+14155552671"
    # Null on missing
    assert result["missing_opt"] is None
    # Metadata included
    assert "_metadata" in result
    assert result["_metadata"]["name"]["confidence"] == 0.9


def test_on_missing_error():
    profile = CanonicalProfile(
        candidate_id="abc",
        full_name="Jane Doe",
        emails=[],
        phones=[],
        location={"city": None, "region": None, "country": None},
        links={"linkedin": None, "github": None, "portfolio": None, "other": []},
        headline=None,
        years_experience=None,
        skills=[],
        experience=[],
        education=[],
        overall_confidence=0.5,
        provenance=[],
        field_metadata={}
    )

    config = {
        "fields": [
            {"target": "email", "from": "emails[0]", "type": "str", "required": True, "on_missing": "error"}
        ]
    }
    projector = ConfigurableProjector(config)

    with pytest.raises(MissingFieldError, match="Required field 'email'"):
        projector.project(profile)


def test_literal_example_config():
    profile = CanonicalProfile(
        candidate_id="abc",
        full_name="Jane Doe",
        emails=["jane@example.com"],
        phones=["4155552671"],
        location={"city": "SF", "region": "CA", "country": "US"},
        links={"linkedin": None, "github": None, "portfolio": None, "other": []},
        headline="Developer",
        years_experience=5.0,
        skills=[
            {"name": "python", "confidence": 0.9, "sources": ["csv"]},
            {"name": "react", "confidence": 0.8, "sources": ["csv"]}
        ],
        experience=[],
        education=[],
        overall_confidence=0.9,
        provenance=[],
        field_metadata={}
    )
    config = {
        "fields": [
            {"path": "full_name", "type": "string", "required": True},
            {"path": "primary_email", "from": "emails[0]", "type": "string", "required": True},
            {"path": "phone", "from": "phones[0]", "type": "string", "normalize": "E164"},
            {"path": "skills", "from": "skills[].name", "type": "string[]", "normalize": "canonical"}
        ],
        "include_confidence": True,
        "on_missing": "null"
    }
    projector = ConfigurableProjector(config)
    result = projector.project(profile)
    
    assert result["full_name"] == "Jane Doe"
    assert result["primary_email"] == "jane@example.com"
    # phone should be E164 normalized
    assert result["phone"] == "+14155552671" or result["phone"] == "+914155552671"
    # skills should be extracted and normalized to canonical (title-cased by taxonomy)
    assert result["skills"] == ["Python", "React"]


def test_contradiction_guard():
    """Verify that required: True meets on_missing: omit fails dynamic validation."""
    profile = CanonicalProfile(
        candidate_id="abc",
        full_name="Jane Doe",
        emails=[],
        phones=[],
        location={"city": None, "region": None, "country": None},
        links={"linkedin": None, "github": None, "portfolio": None, "other": []},
        headline=None,
        years_experience=None,
        skills=[],
        experience=[],
        education=[],
        overall_confidence=0.5,
        provenance=[],
        field_metadata={}
    )

    config = {
        "fields": [
            {
                "target": "must_have", 
                "from": "missing_field", 
                "type": "str", 
                "required": True, 
                "on_missing": "omit"  # <--- Contradiction! It's required but omitted.
            }
        ]
    }
    
    projector = ConfigurableProjector(config)

    with pytest.raises(ValidationError) as exc_info:
        projector.project(profile)
    
    assert "must_have" in str(exc_info.value)
    assert "Field required" in str(exc_info.value)
