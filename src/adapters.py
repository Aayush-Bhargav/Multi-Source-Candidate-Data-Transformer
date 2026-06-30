"""
adapters.py — Production-isolated ingest adapters for candidate data sources.

Each adapter yields ``CandidateFragment`` instances inside a localised
``try / except``.  Corrupted or missing files **never** crash the runtime;
they yield an empty fragment tagged ``source_identifier='failed:<type>'``.

Key design decisions
────────────────────
• ``_fuzzy_extract`` — scans dict keys via case-insensitive regex so that
  adapters tolerate unpredictable column / key names across sources.
• ``ats_json_adapter`` uses **ijson** for O(1)-memory streaming of massive
  JSON arrays.
• ``resume_adapter`` uses deterministic regex only — zero LLM calls.
• ``github_adapter`` has 404 handling, 403 exponential backoff, and an
  ``OFFLINE_MODE`` fallback for demos.

  **Note on GitHub enrichment**: Without a ``candidate_hint`` parameter
  linking the GitHub username to a known email or candidate_id, GitHub
  fragments will form isolated entity-resolution groups since the API
  rarely exposes a public email or phone.
"""

from __future__ import annotations

import csv
import json
import logging
import os
import re
import time
from pathlib import Path
from typing import Any, Generator

import fitz  # PyMuPDF
import ijson
import requests

from src.schema import CandidateFragment

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Shared helpers
# ─────────────────────────────────────────────────────────────────────────────

def _failed_fragment(source_type: str, detail: str = "") -> CandidateFragment:
    """Return an empty CandidateFragment tagged as a failed ingest."""
    return CandidateFragment(
        source_identifier=f"failed:{source_type}",
        raw_extracted_id=detail or None,
    )


def _fuzzy_extract(
    data: dict[str, Any],
    pattern: str,
) -> Any | None:
    """Scan *data*'s keys with a case-insensitive regex and return the first
    matching value.

    Parameters
    ----------
    data : dict[str, Any]
        The dictionary to search (e.g. a CSV row or a JSON record).
    pattern : str
        A regex fragment applied against each key (e.g. ``r"mail"``).

    Returns
    -------
    Any | None
        The value of the first key that matches, or ``None``.
    """
    rx = re.compile(pattern, re.IGNORECASE)
    for key, value in data.items():
        if rx.search(key):
            return value
    return None


def _fuzzy_extract_list(data: dict[str, Any], pattern: str) -> list[str]:
    """Like ``_fuzzy_extract`` but always returns a ``list[str]``."""
    raw = _fuzzy_extract(data, pattern)
    if raw is None:
        return []
    if isinstance(raw, list):
        return [str(v) for v in raw if v]
    if isinstance(raw, str):
        # Split on common delimiters: semicolons, commas, pipes
        return [s.strip() for s in re.split(r"[;|,]+", raw) if s.strip()]
    return [str(raw)]


def _fuzzy_extract_dict_list(
    data: dict[str, Any], pattern: str,
) -> list[dict[str, Any]]:
    """Like ``_fuzzy_extract`` but coerces the result to ``list[dict]``."""
    raw = _fuzzy_extract(data, pattern)
    if isinstance(raw, list):
        return [r for r in raw if isinstance(r, dict)]
    return []


# ─────────────────────────────────────────────────────────────────────────────
# 1. CSV adapter — Recruiter CSV source
# ─────────────────────────────────────────────────────────────────────────────

def csv_adapter(
    file_path: str | Path,
) -> Generator[CandidateFragment, None, None]:
    """Lazily stream rows from a recruiter CSV into ``CandidateFragment``s.

    Column names are matched heuristically via ``_fuzzy_extract`` so that
    unpredictable header variations (e.g. "E-Mail", "email_address",
    "Candidate Name") are handled without hard-coded key lookups.
    """
    try:
        path = Path(file_path)
        if not path.exists():
            logger.error("csv_adapter: file not found — %s", file_path)
            yield _failed_fragment("csv", f"file_not_found:{file_path}")
            return

        with open(path, encoding="utf-8", newline="") as fh:
            reader = csv.DictReader(fh)
            for row_idx, row in enumerate(reader):
                try:
                    yield _parse_csv_row(row, row_idx)
                except Exception as exc:
                    logger.warning("csv_adapter: row %d — %s", row_idx, exc)
                    yield _failed_fragment("csv_row", f"row_{row_idx}:{exc}")

    except Exception as exc:
        logger.error("csv_adapter: file-level failure — %s", exc)
        yield _failed_fragment("csv", str(exc))


def _parse_csv_row(row: dict[str, str], row_idx: int) -> CandidateFragment:
    """Map a single CSV row to a ``CandidateFragment`` using fuzzy keys."""
    raw_id = _fuzzy_extract(row, r"^id$|candidate.?id|record.?id")
    # Fix #5: Try full.?name first (consistent with ATS adapter robustness),
    # then fall back to the previous pattern.
    name = (
        _fuzzy_extract(row, r"full.?name")
        or _fuzzy_extract(row, r"^(?!.*id$)(?:name|candidate)")
    )
    emails = _fuzzy_extract_list(row, r"mail")
    phones = _fuzzy_extract_list(row, r"phone|mobile|cell|tel")
    location = _fuzzy_extract(row, r"^(?:location|city|address|region)")
    headline = _fuzzy_extract(row, r"headline|title|position|role")
    skills = _fuzzy_extract_list(row, r"skill|technology|stack|competency")
    linkedin = _fuzzy_extract(row, r"linkedin")

    # Fix #4: Also extract current_company and title into experience_raw.
    current_company = _fuzzy_extract(row, r"company|employer|organization|org")
    title_val = _fuzzy_extract(row, r"title|position|role|designation")

    experience_raw: list[dict[str, Any]] = []
    if current_company or title_val:
        experience_raw.append({
            "company": str(current_company).strip() if current_company else "Unspecified",
            "title": str(title_val).strip() if title_val else "Unspecified",
            "start": None,
            "end": None,
            "summary": "",
        })

    links: dict[str, Any] | None = None
    if linkedin and str(linkedin).strip():
        links = {"linkedin": str(linkedin).strip()}

    has_real_id = raw_id is not None and str(raw_id).strip() != ""

    return CandidateFragment(
        source_identifier="csv",
        raw_extracted_id=str(raw_id).strip() if has_real_id else f"row_{row_idx}",
        id_is_synthetic=not has_real_id,
        full_name=str(name).strip() if name else None,
        emails=emails,
        phones=phones,
        location_raw=str(location).strip() if location else None,
        headline=str(headline).strip() if headline else None,
        skills_raw=skills,
        experience_raw=experience_raw,
        links_raw=links,
    )


# ─────────────────────────────────────────────────────────────────────────────
# 2. ATS JSON adapter — ijson streaming, zero json.load()
# ─────────────────────────────────────────────────────────────────────────────

def ats_json_adapter(
    file_path: str | Path,
) -> Generator[CandidateFragment, None, None]:
    """Lazily stream records from an ATS JSON export using **ijson**.

    If the file root is a JSON array, records are streamed one-by-one via
    ``ijson.items(fh, 'item')``.  If it is a single JSON object, it is
    processed as an isolated record.

    Memory usage is O(1) per record — safe for multi-GB ATS exports.
    """
    try:
        path = Path(file_path)
        if not path.exists():
            logger.error("ats_json_adapter: not found — %s", file_path)
            yield _failed_fragment("ats_json", f"file_not_found:{file_path}")
            return

        # Peek at the first non-whitespace byte to detect array vs object.
        with open(path, "rb") as peek_fh:
            first_byte = b""
            while True:
                ch = peek_fh.read(1)
                if not ch:
                    break
                if not ch.isspace():
                    first_byte = ch
                    break

        if first_byte == b"[":
            # Stream array elements lazily via ijson
            with open(path, "rb") as fh:
                for idx, record in enumerate(ijson.items(fh, "item")):
                    try:
                        yield _parse_ats_record(record, idx)
                    except Exception as exc:
                        logger.warning(
                            "ats_json_adapter: record %d — %s", idx, exc,
                        )
                        yield _failed_fragment(
                            "ats_json_record", f"record_{idx}:{exc}",
                        )
        elif first_byte == b"{":
            # Single object — read once
            with open(path, encoding="utf-8") as fh:
                record = json.load(fh)
            yield _parse_ats_record(record, 0)
        else:
            yield _failed_fragment(
                "ats_json", f"unexpected_root_byte:{first_byte!r}",
            )

    except Exception as exc:
        logger.error("ats_json_adapter: failure — %s", exc)
        yield _failed_fragment("ats_json", str(exc))


def _parse_ats_record(
    record: dict[str, Any], idx: int,
) -> CandidateFragment:
    """Map a single ATS JSON object using fuzzy key extraction."""
    raw_id = _fuzzy_extract(record, r"^id$|candidate.?id|record.?id")
    name = (
        _fuzzy_extract(record, r"full.?name")
        or _fuzzy_extract(record, r"^(?!.*id$)(?:name|candidate)")
    )
    emails = _fuzzy_extract_list(record, r"mail")
    phones = _fuzzy_extract_list(record, r"phone|mobile|cell|tel")
    location = _fuzzy_extract(record, r"^(?!.*(?:email|mail))(?:location|city|address|region)")
    headline = (
        _fuzzy_extract(record, r"headline")
        or _fuzzy_extract(record, r"title|position|role")
    )
    skills = _fuzzy_extract_list(record, r"skill|technolog|stack|competenc")
    experience = _fuzzy_extract_dict_list(
        record, r"employ|exper|work|history|career",
    )
    education = _fuzzy_extract_dict_list(record, r"educat|academ|degree|school")
    links_raw = _fuzzy_extract(record, r"link|url|social|profile")
    links_dict: dict[str, Any] | None = None
    
    def _extract_link(pat: str) -> str | None:
        v = _fuzzy_extract(record, pat)
        if v and isinstance(v, str): return v.strip()
        if isinstance(links_raw, dict):
            v = _fuzzy_extract(links_raw, pat)
            if v and isinstance(v, str): return v.strip()
        return None
        
    li = _extract_link(r"linkedin")
    gh = _extract_link(r"github")
    pf = _extract_link(r"portfolio|website|blog")
    if li or gh or pf:
        links_dict = {"linkedin": li, "github": gh, "portfolio": pf, "other": []}

    has_real_id = raw_id is not None and str(raw_id).strip() != ""

    return CandidateFragment(
        source_identifier="ats_json",
        raw_extracted_id=str(raw_id) if has_real_id else f"record_{idx}",
        id_is_synthetic=not has_real_id,
        full_name=str(name).strip() if name else None,
        emails=emails,
        phones=phones,
        location_raw=str(location).strip() if location else None,
        headline=str(headline).strip() if headline else None,
        skills_raw=skills,
        experience_raw=experience,
        education_raw=education,
        links_raw=links_dict,
    )


# ─────────────────────────────────────────────────────────────────────────────
# 3. Resume (PDF) adapter — deterministic regex, zero LLM calls
# ─────────────────────────────────────────────────────────────────────────────

_RE_EMAIL = re.compile(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}")
_RE_PHONE = re.compile(
    r"(?:\+?\d{1,3}[\s\-.]?)?\(?\d{2,4}\)?[\s\-.]?\d{3,4}[\s\-.]?\d{3,4}",
)
_RE_LINKEDIN = re.compile(
    r"(?:https?://)?(?:www\.)?linkedin\.com/in/[\w\-]+/?", re.IGNORECASE,
)
_RE_GITHUB = re.compile(
    r"(?:https?://)?(?:www\.)?github\.com/[\w\-]+/?", re.IGNORECASE,
)
_RE_URL = re.compile(r"https?://[^\s,;\"'<>\])}]+")

# Hyper-resilient section header patterns
_SECTION_PATTERNS: dict[str, re.Pattern[str]] = {
    "experience": re.compile(
        r"^(?:work\s+)?experience|employment\s*(?:history)?|"
        r"professional\s+(?:experience|background|history)|"
        r"career\s*(?:history|summary)?|work\s+history|"
        r"relevant\s+experience|background",
        re.IGNORECASE,
    ),
    "education": re.compile(
        r"^education|academic|qualifications?|certifications?|"
        r"degrees?|training|scholastic",
        re.IGNORECASE,
    ),
    "skills": re.compile(
        r"^(?:technical\s+)?skills|core\s+competenc|"
        r"technologies|proficienc|expertise|"
        r"tools?\s*(?:&|and)?\s*technologies|tech\s+stack|"
        r"programming|languages?\s+(?:&|and)\s+frameworks?",
        re.IGNORECASE,
    ),
    "projects": re.compile(
        r"^projects?|personal\s+projects?|portfolio|"
        r"key\s+projects?|notable\s+work",
        re.IGNORECASE,
    ),
}


def resume_adapter(
    file_path: str | Path,
) -> Generator[CandidateFragment, None, None]:
    """Extract candidate data from a PDF résumé via PyMuPDF + regex.

    **No LLM calls** — fully deterministic.
    """
    try:
        path = Path(file_path)
        if not path.exists():
            logger.error("resume_adapter: not found — %s", file_path)
            yield _failed_fragment("resume", f"file_not_found:{file_path}")
            return

        doc = fitz.open(str(path))
        full_text = "\n".join(page.get_text() for page in doc)
        doc.close()

        if not full_text.strip():
            yield _failed_fragment("resume", "empty_text")
            return

        yield _parse_resume_text(full_text, str(path))

    except Exception as exc:
        logger.error("resume_adapter: failed — %s", exc)
        yield _failed_fragment("resume", str(exc))


def _parse_resume_text(text: str, path_str: str) -> CandidateFragment:
    """Deterministic regex-based parser for raw résumé text."""
    lines = text.splitlines()

    # Contact info
    emails = list(dict.fromkeys(_RE_EMAIL.findall(text)))
    phones = list(dict.fromkeys(_RE_PHONE.findall(text)))
    links: dict[str, Any] = {}
    li = _RE_LINKEDIN.findall(text)
    gh = _RE_GITHUB.findall(text)
    other = [
        u for u in _RE_URL.findall(text)
        if "linkedin.com" not in u.lower() and "github.com" not in u.lower()
    ]
    if li:
        links["linkedin"] = li[0]
    if gh:
        links["github"] = gh[0]
    if other:
        links["other"] = other

    # Name heuristic: first non-trivial line that isn't contact info
    full_name: str | None = None
    for line in lines:
        s = line.strip()
        if not s or _RE_EMAIL.search(s) or _RE_URL.search(s):
            continue
        if _RE_PHONE.fullmatch(s):
            continue
        full_name = s
        break

    # Section splitting
    sections: dict[str, list[str]] = {}
    current: str | None = None
    for line in lines:
        s = line.strip()
        matched = False
        for name, pat in _SECTION_PATTERNS.items():
            if pat.search(s):
                current = name
                sections.setdefault(current, [])
                matched = True
                break
        if not matched and current and s:
            sections.setdefault(current, []).append(s)

    # Skills
    skills_raw: list[str] = []
    for sl in sections.get("skills", []):
        skills_raw.extend(
            t.strip()
            for t in re.split(r"[,|•;·\u2022\u2023\u25E6\u2043]+", sl)
            if t.strip()
        )

    # Experience / education blocks
    experience_raw = _group_blocks(sections.get("experience", []))
    education_raw = _group_blocks(sections.get("education", []))

    headline: str | None = None
    exp = sections.get("experience", [])
    if exp:
        headline = exp[0]

    return CandidateFragment(
        source_identifier="resume",
        raw_extracted_id=Path(path_str).stem,
        id_is_synthetic=True,
        full_name=full_name,
        emails=emails,
        phones=phones,
        headline=headline,
        skills_raw=skills_raw,
        experience_raw=experience_raw,
        education_raw=education_raw,
        links_raw=links or None,
    )


# Date-range detection for resume block parsing (fix #6).
# Matches patterns like "Jan 2020 - Present", "2019-01 - 2020-06",
# "January 2020 to December 2021", "2020 — Present", etc.
_RE_DATE_RANGE = re.compile(
    r"(?:(?:(?:" + r"|".join(re.escape(name) for name in __import__("calendar").month_name if name)
    + r")[,]?\s+\d{4}|\d{4}[-/]\d{2}|\d{2}[-/]\d{4}|\d{4}))"
    r"\s*(?:[-–—]|to)\s*"
    r"(?:(?:" + r"|".join(re.escape(name) for name in __import__("calendar").month_name if name)
    + r")[,]?\s+\d{4}|\d{4}[-/]\d{2}|\d{2}[-/]\d{4}|\d{4}|[Pp]resent|[Cc]urrent)",
    re.IGNORECASE,
)
# Title-company split: "Title at Company", "Title, Company", "Title — Company"
_RE_TITLE_COMPANY = re.compile(
    r"^(.+?)\s+(?:at|@)\s+(.+)$"
    r"|^(.+?)\s*[,]\s+(.+)$"
    r"|^(.+?)\s*[–—]\s+(.+)$",
    re.IGNORECASE,
)


def _group_blocks(lines: list[str]) -> list[dict[str, Any]]:
    """Group consecutive lines into blocks, attempting date/title/company extraction.

    Within each block of raw_lines, look for a date-range line.  If found,
    try to parse title/company from the remaining lines using common
    'Title at Company' / 'Title, Company' / 'Title — Company' patterns.
    Falls back to 'Unspecified' only when no lines remain after date removal.
    """
    blocks: list[dict[str, Any]] = []
    buf: list[str] = []
    for line in lines:
        if not line.strip():
            if buf:
                blocks.append(_parse_block(buf))
                buf = []
        else:
            buf.append(line.strip())
    if buf:
        blocks.append(_parse_block(buf))
    return blocks


def _parse_block(raw_lines: list[str]) -> dict[str, Any]:
    """Attempt structured extraction from a block of resume lines."""
    date_line_idx: int | None = None
    start_str: str | None = None
    end_str: str | None = None

    for idx, line in enumerate(raw_lines):
        m = _RE_DATE_RANGE.search(line)
        if m:
            date_line_idx = idx
            # Split the matched text on separator to get start/end
            sep_m = re.split(r"\s*(?:[-–—]|to)\s*", m.group(0), maxsplit=1)
            if len(sep_m) == 2:
                start_str = sep_m[0].strip()
                end_str = sep_m[1].strip()
            break

    if date_line_idx is not None:
        remaining = [l for i, l in enumerate(raw_lines) if i != date_line_idx]
    else:
        remaining = raw_lines[:]

    company: str = "Unspecified"
    title: str = "Unspecified"

    if remaining:
        # Try to split first remaining line as "Title at/,/— Company"
        tc = _RE_TITLE_COMPANY.match(remaining[0])
        if tc:
            groups = tc.groups()
            # 3 alternatives in the regex, each producing 2 groups
            for i in range(0, len(groups), 2):
                if groups[i] is not None:
                    title = groups[i].strip()
                    company = groups[i + 1].strip()
                    break
        elif len(remaining) >= 2:
            title = remaining[0]
            company = remaining[1]
        else:
            title = remaining[0]

    if date_line_idx is not None:
        return {
            "company": company,
            "title": title,
            "start": start_str,
            "end": end_str,
            "summary": " | ".join(raw_lines),
        }
    else:
        # No date found — fall back to raw_lines dict for downstream handling
        return {"raw_lines": raw_lines}


# ─────────────────────────────────────────────────────────────────────────────
# 4. GitHub adapter — REST API, auth, rate-limit backoff, offline mode
# ─────────────────────────────────────────────────────────────────────────────

_GITHUB_API = "https://api.github.com"
_TIMEOUT = 15
_MAX_RETRIES = 3


def github_adapter(
    username: str,
    *,
    mock_file: str | Path | None = None,
    candidate_hint: dict[str, str] | None = None,
) -> Generator[CandidateFragment, None, None]:
    """Ingest candidate metadata from the GitHub REST API.

    Mapping: location→location_raw, bio→headline, repo languages→skills_raw,
    profile URL→links_raw["github"].

    Parameters
    ----------
    candidate_hint : dict | None
        Optional dict with keys ``"email"`` and/or ``"candidate_id"``.
        When provided, ``email`` is injected into the fragment's emails
        list and ``candidate_id`` is set as ``raw_extracted_id``, giving
        the downstream entity resolver a basis to link this fragment to
        the correct candidate group.  Without this hint, GitHub fragments
        will almost always form isolated groups since the API rarely
        exposes a public email or phone.
    """
    try:
        offline = os.environ.get("OFFLINE_MODE", "").lower() in (
            "true", "1", "yes",
        )
        if offline:
            yield from _github_offline(username, mock_file, candidate_hint)
            return

        headers = _gh_headers()

        profile = _gh_get(f"{_GITHUB_API}/users/{username}", headers=headers)
        if profile is None:
            yield _failed_fragment("github", f"user_not_found:{username}")
            return

        repos = _gh_get(
            f"{_GITHUB_API}/users/{username}/repos?per_page=100&sort=pushed",
            headers=headers,
        )
        langs: set[str] = set()
        if isinstance(repos, list):
            for r in repos:
                lang = r.get("language")
                if lang:
                    langs.add(lang)

        yield _build_gh_fragment(profile, sorted(langs), username, candidate_hint)

    except Exception as exc:
        logger.error("github_adapter: failure — %s", exc)
        yield _failed_fragment("github", str(exc))


def _gh_headers() -> dict[str, str]:
    headers: dict[str, str] = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    token = os.environ.get("GITHUB_TOKEN", "").strip()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def _gh_get(url: str, *, headers: dict[str, str]) -> Any | None:
    """GET with 404 handling and 403 exponential backoff."""
    for attempt in range(1, _MAX_RETRIES + 1):
        resp = requests.get(url, headers=headers, timeout=_TIMEOUT)
        if resp.status_code == 200:
            return resp.json()
        if resp.status_code == 404:
            logger.warning("github 404: %s", url)
            return None
        if resp.status_code == 403:
            wait = 5 * (2 ** (attempt - 1))
            reset = resp.headers.get("X-RateLimit-Reset")
            if reset:
                try:
                    wait = max(wait, int(reset) - int(time.time()) + 1)
                except (ValueError, TypeError):
                    pass
            logger.warning("github 403: backoff %ds (attempt %d)", wait, attempt)
            time.sleep(wait)
            continue
        resp.raise_for_status()
    return None


def _github_offline(
    username: str, mock_file: str | Path | None,
    candidate_hint: dict[str, str] | None = None,
) -> Generator[CandidateFragment, None, None]:
    if mock_file is None:
        mock_file = (
            Path(__file__).resolve().parent.parent
            / "data" / "sample_inputs" / f"github_{username}.json"
        )
    path = Path(mock_file)
    if not path.exists():
        yield _failed_fragment("github_offline", f"mock_not_found:{path}")
        return
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        profile = data.get("profile", data)
        langs = sorted(set(data.get("languages", [])))
        yield _build_gh_fragment(profile, langs, username, candidate_hint)
    except Exception as exc:
        yield _failed_fragment("github_offline", str(exc))


def _build_gh_fragment(
    profile: dict[str, Any], languages: list[str], username: str,
    candidate_hint: dict[str, str] | None = None,
) -> CandidateFragment:
    emails: list[str] = []
    email = profile.get("email")
    if email:
        emails = [email]

    raw_id = str(profile.get("id", username))

    # Fix #7: Inject candidate_hint for entity resolution linkage.
    if candidate_hint:
        hint_email = candidate_hint.get("email")
        if hint_email and hint_email not in emails:
            emails.append(hint_email)
        hint_id = candidate_hint.get("candidate_id")
        if hint_id:
            raw_id = hint_id

    return CandidateFragment(
        source_identifier="github",
        raw_extracted_id=raw_id,
        full_name=profile.get("name"),
        emails=emails,
        location_raw=profile.get("location"),
        headline=profile.get("bio"),
        skills_raw=languages,
        links_raw={
            "github": profile.get(
                "html_url", f"https://github.com/{username}",
            ),
        },
    )
