import pytest
from src.pipeline import stream_fragments, accumulate_fragments
from src.schema import CandidateFragment

def mock_adapter_1(source_path):
    yield CandidateFragment(
        source_identifier="mock1",
        raw_extracted_id="id1",
        full_name="Alice"
    )

def mock_adapter_2(source_path):
    yield CandidateFragment(
        source_identifier="mock2",
        raw_extracted_id="id2",
        full_name="Bob"
    )

def mock_raising_adapter(source_path):
    raise ValueError("Something went wrong")
    yield  # generator

def test_stream_fragments():
    sources = [
        (mock_adapter_1, "path1"),
        (mock_adapter_2, "path2")
    ]
    fragments = list(stream_fragments(sources))
    assert len(fragments) == 2
    assert fragments[0]["source_identifier"] == "mock1"
    assert fragments[0]["raw_extracted_id"] == "id1"
    assert fragments[0]["full_name"] == "Alice"
    assert fragments[1]["source_identifier"] == "mock2"
    assert fragments[1]["raw_extracted_id"] == "id2"
    assert fragments[1]["full_name"] == "Bob"

def test_stream_fragments_with_raising_adapter():
    sources = [
        (mock_adapter_1, "path1"),
        (mock_raising_adapter, "path2")
    ]
    fragments = list(stream_fragments(sources))
    assert len(fragments) == 2
    assert fragments[0]["source_identifier"] == "mock1"
    # The raising adapter should yield a failed fragment instead of crashing
    assert fragments[1]["source_identifier"] == "failed:mock_raising_adapter"
    assert "Something went wrong" in fragments[1]["raw_extracted_id"]

def test_accumulate_fragments():
    sources = [
        (mock_adapter_1, "path1"),
        (mock_adapter_2, "path2")
    ]
    fragments = accumulate_fragments(sources)
    assert isinstance(fragments, list)
    assert len(fragments) == 2
    assert fragments[0]["source_identifier"] == "mock1"
    assert fragments[1]["source_identifier"] == "mock2"
