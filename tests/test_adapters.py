import os
import json
import pytest
from unittest.mock import patch, MagicMock
import fitz  # PyMuPDF
from src.adapters import csv_adapter, ats_json_adapter, resume_adapter, github_adapter
from src.schema import CandidateFragment

def test_csv_adapter_success(tmp_path):
    csv_file = tmp_path / "recruiter.csv"
    content = (
        "id,Candidate Name,email_address,mobile,location,headline,skills_list,linkedin_url\n"
        "C001,Jane Doe,jane@example.com,+14155552671,\"San Francisco, CA\",Senior Developer,\"Python, React\",linkedin.com/in/jane\n"
    )
    csv_file.write_text(content, encoding="utf-8")

    frags = list(csv_adapter(csv_file))
    assert len(frags) == 1
    f = frags[0]
    assert f.source_identifier == "csv"
    assert f.raw_extracted_id == "C001"
    assert f.full_name == "Jane Doe"
    assert f.emails == ["jane@example.com"]
    assert f.phones == ["+14155552671"]
    assert f.location_raw == "San Francisco, CA"
    assert f.headline == "Senior Developer"
    assert f.skills_raw == ["Python", "React"]
    assert f.links_raw == {"linkedin": "linkedin.com/in/jane"}

def test_csv_adapter_missing_file():
    frags = list(csv_adapter("non_existent_file.csv"))
    assert len(frags) == 1
    assert frags[0].source_identifier == "failed:csv"
    assert "file_not_found" in frags[0].raw_extracted_id

def test_ats_json_adapter_array(tmp_path):
    json_file = tmp_path / "ats.json"
    data = [
        {
            "candidateId": "ATS001",
            "name": "John Smith",
            "emails": ["john@example.com"],
            "phones": ["+12125551234"],
            "location": "New York",
            "title": "Backend Dev",
            "skills": ["Go", "Kubernetes"],
            "workHistory": [{"company": "TechCorp", "title": "SE"}],
            "education": [{"school": "NYU"}],
            "links": {"github": "github.com/john"}
        }
    ]
    json_file.write_text(json.dumps(data), encoding="utf-8")

    frags = list(ats_json_adapter(json_file))
    assert len(frags) == 1
    f = frags[0]
    assert f.source_identifier == "ats_json"
    assert f.raw_extracted_id == "ATS001"
    assert f.full_name == "John Smith"
    assert f.emails == ["john@example.com"]
    assert f.skills_raw == ["Go", "Kubernetes"]
    assert f.links_raw == {"linkedin": None, "github": "github.com/john", "portfolio": None, "other": []}

def test_ats_json_adapter_single_object(tmp_path):
    json_file = tmp_path / "ats_single.json"
    data = {
        "candidateId": "ATS002",
        "name": "Alice",
        "emails": ["alice@example.com"]
    }
    json_file.write_text(json.dumps(data), encoding="utf-8")

    frags = list(ats_json_adapter(json_file))
    assert len(frags) == 1
    assert frags[0].raw_extracted_id == "ATS002"
    assert frags[0].full_name == "Alice"

def test_ats_json_adapter_missing_file():
    frags = list(ats_json_adapter("non_existent_file.json"))
    assert len(frags) == 1
    assert frags[0].source_identifier == "failed:ats_json"

def test_resume_adapter_success(tmp_path):
    pdf_file = tmp_path / "resume.pdf"
    
    # Generate a simple PDF dynamically with fitz
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((50, 50), "Jane Smith\nEmail: janesmith@example.com\nPhone: +1 555 123 4567\n\nProfessional Background\nSenior Software Engineer at Apple\nBuilt iOS apps.\n\nScholastic Training\nBS CS from Stanford University\n\nCore Competencies\nSwift, Objective-C, Cocoa")
    doc.save(str(pdf_file))
    doc.close()

    frags = list(resume_adapter(pdf_file))
    assert len(frags) == 1
    f = frags[0]
    assert f.source_identifier == "resume"
    assert f.full_name == "Jane Smith"
    assert f.emails == ["janesmith@example.com"]
    assert f.phones == ["+1 555 123 4567"]
    assert f.skills_raw == ["Swift", "Objective-C", "Cocoa"]
    assert len(f.experience_raw) > 0
    assert len(f.education_raw) > 0

def test_resume_adapter_missing_file():
    frags = list(resume_adapter("non_existent_resume.pdf"))
    assert len(frags) == 1
    assert frags[0].source_identifier == "failed:resume"

def test_github_adapter_offline(tmp_path):
    mock_file = tmp_path / "github_octocat.json"
    data = {
        "profile": {
            "id": 5832347,
            "login": "octocat",
            "name": "The Octocat",
            "email": "octocat@github.com",
            "bio": "Mascot",
            "location": "San Francisco",
            "html_url": "https://github.com/octocat"
        },
        "languages": ["Ruby", "Python"]
    }
    mock_file.write_text(json.dumps(data), encoding="utf-8")

    # Set OFFLINE_MODE env var
    with patch.dict(os.environ, {"OFFLINE_MODE": "true"}):
        frags = list(github_adapter("octocat", mock_file=mock_file))
        assert len(frags) == 1
        f = frags[0]
        assert f.source_identifier == "github"
        assert f.raw_extracted_id == "5832347"
        assert f.full_name == "The Octocat"
        assert f.emails == ["octocat@github.com"]
        assert f.location_raw == "San Francisco"
        assert f.headline == "Mascot"
        assert f.skills_raw == ["Python", "Ruby"]
        assert f.links_raw == {"github": "https://github.com/octocat"}

@patch("requests.get")
def test_github_adapter_live_success(mock_get):
    # Mock profile response
    profile_response = MagicMock()
    profile_response.status_code = 200
    profile_response.json.return_type = {}
    profile_response.json.value = {
        "id": 9999,
        "login": "testuser",
        "name": "Test User",
        "email": "test@user.com",
        "bio": "Hello World",
        "location": "London",
        "html_url": "https://github.com/testuser"
    }
    
    # Mock repos response
    repos_response = MagicMock()
    repos_response.status_code = 200
    repos_response.json.return_type = []
    repos_response.json.value = [
        {"language": "Python"},
        {"language": "Go"},
        {"language": "Python"}  # duplicate language
    ]

    def mock_get_side_effect(url, **kwargs):
        if "/repos" in url:
            repos_response.json.return_value = repos_response.json.value
            return repos_response
        else:
            profile_response.json.return_value = profile_response.json.value
            return profile_response

    mock_get.side_effect = mock_get_side_effect

    with patch.dict(os.environ, {"OFFLINE_MODE": "false", "GITHUB_TOKEN": "test_token"}):
        frags = list(github_adapter("testuser"))
        assert len(frags) == 1
        f = frags[0]
        assert f.source_identifier == "github"
        assert f.raw_extracted_id == "9999"
        assert f.full_name == "Test User"
        assert f.emails == ["test@user.com"]
        assert f.skills_raw == ["Go", "Python"]
        assert f.links_raw == {"github": "https://github.com/testuser"}

@patch("requests.get")
def test_github_adapter_live_404(mock_get):
    mock_resp = MagicMock()
    mock_resp.status_code = 404
    mock_get.return_value = mock_resp

    with patch.dict(os.environ, {"OFFLINE_MODE": "false"}):
        frags = list(github_adapter("unknownuser"))
        assert len(frags) == 1
        assert frags[0].source_identifier == "failed:github"
        assert "user_not_found" in frags[0].raw_extracted_id

def test_ats_json_adapter_links_canonical(tmp_path):
    json_file = tmp_path / "ats_links.json"
    data = {
        "candidateId": "ATS_LINKS",
        "name": "Link Tester",
        # Non-canonical or flat link field names, mapped by the fuzzy extractor
        "linkedin_url": "linkedin.com/in/test",
        "github_profile": "github.com/test",
        "portfolio_site": "blog.test.com",
        "random_junk": "ignore.com"
    }
    json_file.write_text(json.dumps(data), encoding="utf-8")

    frags = list(ats_json_adapter(json_file))
    assert len(frags) == 1
    f = frags[0]
    assert f.links_raw == {
        "linkedin": "linkedin.com/in/test",
        "github": "github.com/test",
        "portfolio": "blog.test.com",
        "other": []
    }
