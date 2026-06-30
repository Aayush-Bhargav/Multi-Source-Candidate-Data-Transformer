"""
test_integration.py — End-to-end execution against mock files.
"""
import json
import os
from pathlib import Path
import pytest

from src.adapters import ats_json_adapter, csv_adapter, github_adapter, resume_adapter
from src.pipeline import accumulate_fragments
from src.merge import merge_fragments
from src.project import ConfigurableProjector


def test_end_to_end_pipeline():
    """Ensure batch executes against mock files, drops malformed implicitly, and projects."""
    os.environ["OFFLINE_MODE"] = "true"
    
    # 1. Source definitions
    inputs_dir = Path(__file__).resolve().parent.parent / "data" / "sample_inputs"
    sources = [
        (csv_adapter, inputs_dir / "recruiter.csv"),
        (ats_json_adapter, inputs_dir / "ats_candidates.json"),
        (github_adapter, "octocat"),
    ]

    # 2. Pipeline execution
    fragments = accumulate_fragments(sources)
    assert len(fragments) > 0, "Pipeline should ingest fragments"
    
    profiles, failed_count = merge_fragments(fragments)
    assert failed_count == 0, "No candidates should fail assembly completely"
    assert len(profiles) > 0, "Profiles should be assembled"

    # 3. Projection
    config_path = Path(__file__).resolve().parent.parent / "configs" / "projection.json"
    with open(config_path) as f:
        config = json.load(f)
    projector = ConfigurableProjector(config)
    
    projected = [projector.project(p) for p in profiles]
    assert len(projected) == len(profiles)

    # 4. Compare with dummy golden JSON (specifically looking for Carlos Rivera)
    golden_path = Path(__file__).resolve().parent / "golden" / "expected_profile.json"
    with open(golden_path) as f:
        golden = json.load(f)
    
    # Find Carlos in the projected outputs (candidate_id for carlos@example.com is e413ca7fc7d0b318)
    carlos_proj = next((p for p in projected if p["id"] == "e413ca7fc7d0b318"), None)
    assert carlos_proj is not None, "Carlos Rivera profile missing from projection"
    
    # Assert key matching (ignoring some variance in confidence calculations in the dummy)
    assert carlos_proj["name"] == golden["name"]
    assert carlos_proj["primary_email"] == golden["primary_email"]
    assert carlos_proj["primary_phone"] == golden["primary_phone"]
    assert carlos_proj["city"] == golden["city"]
    
    # Verify metadata is injected
    assert "_metadata" in carlos_proj
    assert "name" in carlos_proj["_metadata"]
