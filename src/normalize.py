"""
normalize.py — Pure, deterministic normalizers for the candidate data pipeline.

Every function in this module is a pure transformer: given an input string,
it returns a ``NormResult`` carrying the (possibly normalised) value together
with metadata that indicates whether normalisation succeeded and any
associated confidence penalty.

Design invariants
─────────────────
1. **No silent data invention** — dates are never back-filled with the
   current execution clock.  The ``dateutil`` default-fill trick is
   intentionally avoided.
2. **No exceptions leak** — malformed inputs are returned verbatim with a
   ``normalized=False`` flag.  Callers never need to ``try/except``.
3. **No side-effects** — every function is referentially transparent.
4. **No schema mutation** — these functions produce new values; they never
   touch Pydantic models directly.
"""

from __future__ import annotations

import calendar
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import phonenumbers
import pycountry
from rapidfuzz import fuzz


# ─────────────────────────────────────────────────────────────────────────────
# Result container
# ─────────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True, slots=True)
class NormResult:
    """Immutable container pairing a normalised value with process metadata.

    Attributes
    ----------
    value : Any
        The normalised output (or the raw input if normalisation failed).
    normalized : bool
        ``True`` when the value was successfully normalised.
    penalty : float
        Confidence penalty to propagate upstream (0.0 = no penalty).
    method : str
        Short tag identifying the code-path that produced this result.
    """

    value: Any
    normalized: bool = True
    penalty: float = 0.0
    method: str = ""


# ─────────────────────────────────────────────────────────────────────────────
# Pre-compiled constants — built once at module import
# ─────────────────────────────────────────────────────────────────────────────

# Month-name → zero-padded number  (e.g. "january" → "01")
_MONTH_MAP: dict[str, str] = {
    name.lower(): f"{idx:02d}"
    for idx, name in enumerate(calendar.month_name)
    if name  # skip the empty 0-th entry
}

# Regex alternation of full month names
_MONTH_NAMES_PATTERN: str = "|".join(
    re.escape(name) for name in calendar.month_name if name
)

# ── Date-format whitelist regexes ──
_RE_MONTH_YYYY = re.compile(
    rf"^({_MONTH_NAMES_PATTERN})[,]?\s+(\d{{4}})$",
    re.IGNORECASE,
)
_RE_YYYY_MM = re.compile(r"^(\d{4})-(0[1-9]|1[0-2])$")
_RE_YYYY_SLASH_MM = re.compile(r"^(\d{4})/(0[1-9]|1[0-2])$")
_RE_MM_SLASH_YYYY = re.compile(r"^(0[1-9]|1[0-2])/(\d{4})$")
_RE_YYYY_ONLY = re.compile(r"^(\d{4})$")

# Sentinel strings that map to ``None`` (end-date = "still there")
_PRESENT_VARIANTS: frozenset[str] = frozenset(
    {"present", "current", "now", "ongoing", ""},
)

# Year extraction for education fields
_RE_FOUR_DIGITS = re.compile(r"(\d{4})")

# Skills taxonomy — lazy-loaded singleton
_taxonomy_cache: list[str] | None = None
_TAXONOMY_PATH: Path = (
    Path(__file__).resolve().parent.parent / "configs" / "skills_taxonomy.json"
)


# ─────────────────────────────────────────────────────────────────────────────
# 1. Phone normaliser
# ─────────────────────────────────────────────────────────────────────────────

def normalize_phone(raw: str, default_region: str | None = None) -> NormResult:
    """Parse a phone string assuming global formats; format to E.164.

    If the raw value already carries an international prefix the library will
    detect it automatically.  When no prefix is present, the *default_region*
    (if provided) is tried first, followed by a hardcoded fallback list
    ordered for an India-centric candidate population: (IN, US, GB).

    On any parse or validation failure the raw string is returned unchanged
    with ``normalized=False``.

    Parameters
    ----------
    raw : str
        The unprocessed phone string.
    default_region : str | None
        Optional ISO-3166 region code to attempt first before the hardcoded
        fallback chain.

    Returns
    -------
    NormResult
        ``.value`` is either the E.164 string or the original *raw* input.
    """
    if not raw or not raw.strip():
        return NormResult(value=raw, normalized=False, method="phone_empty")

    text = raw.strip()

    # Attempt region-free parse first (requires '+' prefix in the string).
    parsed = _try_parse_phone(text, region=None)

    # Fallback: try default_region first, then common regions.
    # Ordered India-first given this candidate population is India-centric.
    if parsed is None:
        fallback_regions: list[str] = []
        if default_region:
            fallback_regions.append(default_region)
        fallback_regions.extend(r for r in ("IN", "US", "GB") if r != default_region)
        for region in fallback_regions:
            parsed = _try_parse_phone(text, region=region)
            if parsed is not None and phonenumbers.is_valid_number(parsed):
                break
        else:
            # None of the fallback regions yielded a valid number.
            if parsed is not None and not phonenumbers.is_valid_number(parsed):
                parsed = None

    if parsed is None or not phonenumbers.is_valid_number(parsed):
        return NormResult(
            value=raw,
            normalized=False,
            method="phone_parse_failed",
        )

    formatted = phonenumbers.format_number(
        parsed, phonenumbers.PhoneNumberFormat.E164,
    )
    return NormResult(value=formatted, normalized=True, method="phone_e164")


def _try_parse_phone(
    text: str,
    *,
    region: str | None,
) -> phonenumbers.PhoneNumber | None:
    """Attempt to parse *text* with the given default *region*.

    Returns ``None`` on any ``NumberParseException``.
    """
    try:
        return phonenumbers.parse(text, region)
    except phonenumbers.NumberParseException:
        return None


# ─────────────────────────────────────────────────────────────────────────────
# 2. Date normaliser (strict whitelist — NO dateutil, NO fuzzy, NO defaults)
# ─────────────────────────────────────────────────────────────────────────────

def normalize_date(raw: str | None) -> NormResult:
    """Strict whitelist-only date normaliser.

    Accepted formats and their outputs::

        Input pattern        Example             Output
        ─────────────        ───────             ──────
        Month YYYY           January 2021   →    '2021-01'
        Month, YYYY          January, 2022  →    '2022-01'
        YYYY-MM              2021-01        →    '2021-01'   (pass-through)
        YYYY/MM              2021/01        →    '2021-01'
        MM/YYYY              01/2021        →    '2021-01'
        YYYY                 2021           →    '2021'      (no month invented)
        Present / Current    Present        →    None
        None / empty         ''             →    None

    Anything else is returned **verbatim** with ``normalized=False``.

    Parameters
    ----------
    raw : str | None
        The date-like string to normalise.

    Returns
    -------
    NormResult
        ``.value`` is a ``str`` in ``'YYYY-MM'`` / ``'YYYY'`` form, ``None``
        for present-sentinels, or the original *raw* input on failure.
    """
    if raw is None:
        return NormResult(value=None, normalized=True, method="date_null")

    text = raw.strip()

    # Present / current / empty → None
    if text.lower() in _PRESENT_VARIANTS:
        return NormResult(value=None, normalized=True, method="date_present")

    # 'Month YYYY' → 'YYYY-MM'
    m = _RE_MONTH_YYYY.match(text)
    if m:
        month_num = _MONTH_MAP[m.group(1).lower()]
        year = m.group(2)
        return NormResult(
            value=f"{year}-{month_num}",
            normalized=True,
            method="date_month_yyyy",
        )

    # 'YYYY-MM' → pass-through
    m = _RE_YYYY_MM.match(text)
    if m:
        return NormResult(value=text, normalized=True, method="date_yyyy_mm")

    # 'YYYY/MM' → normalise to 'YYYY-MM'
    m = _RE_YYYY_SLASH_MM.match(text)
    if m:
        return NormResult(
            value=f"{m.group(1)}-{m.group(2)}",
            normalized=True,
            method="date_yyyy_slash_mm",
        )

    # 'MM/YYYY' → normalise to 'YYYY-MM'
    m = _RE_MM_SLASH_YYYY.match(text)
    if m:
        return NormResult(
            value=f"{m.group(2)}-{m.group(1)}",
            normalized=True,
            method="date_mm_slash_yyyy",
        )

    # 'YYYY' → keep as-is (we refuse to invent a month)
    m = _RE_YYYY_ONLY.match(text)
    if m:
        return NormResult(value=text, normalized=True, method="date_yyyy")

    # No whitelist match — return verbatim, flagged
    return NormResult(
        value=raw,
        normalized=False,
        method="date_unrecognized",
    )


# ─────────────────────────────────────────────────────────────────────────────
# 3. Year normaliser (education end-years)
# ─────────────────────────────────────────────────────────────────────────────

def normalize_year(raw: str | int | None) -> NormResult:
    """Extract a 4-digit year from an education end-year value.

    Parameters
    ----------
    raw : str | int | None
        Anything that might contain a year (e.g. ``'Class of 2018'``,
        ``2020``, ``'2019-06'``).

    Returns
    -------
    NormResult
        ``.value`` is an ``int`` year or ``None`` if unparseable.
    """
    if raw is None:
        return NormResult(value=None, normalized=True, method="year_null")

    text = str(raw).strip()
    if not text:
        return NormResult(value=None, normalized=True, method="year_empty")

    m = _RE_FOUR_DIGITS.search(text)
    if m:
        return NormResult(
            value=int(m.group(1)),
            normalized=True,
            method="year_extracted",
        )

    return NormResult(value=None, normalized=False, method="year_unparseable")


# ─────────────────────────────────────────────────────────────────────────────
# 4. Country normaliser (free-text → ISO-3166 alpha-2)
# ─────────────────────────────────────────────────────────────────────────────

# All 50 US states + DC two-letter abbreviations.
# Guards against ISO-3166 alpha-2 collisions where a US state abbreviation
# is also a valid country code — e.g. CA=Canada, IN=India, DE=Germany,
# CO=Colombia, ME=Montenegro, AL=Albania, GA=Georgia, PA=Panama, etc.
_US_STATE_ABBREVS: frozenset[str] = frozenset({
    "AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "FL", "GA",
    "HI", "ID", "IL", "IN", "IA", "KS", "KY", "LA", "ME", "MD",
    "MA", "MI", "MN", "MS", "MO", "MT", "NE", "NV", "NH", "NJ",
    "NM", "NY", "NC", "ND", "OH", "OK", "OR", "PA", "RI", "SC",
    "SD", "TN", "TX", "UT", "VT", "VA", "WA", "WV", "WI", "WY",
    "DC",
})


def normalize_country(raw: str | None) -> NormResult:
    """Resolve a free-text geographic string to an ISO-3166 alpha-2 code.

    Splits on commas and tries each token (in reverse order, since the
    country portion typically trails the city) against ``pycountry``'s
    exact-lookup and fuzzy-search facilities.

    When the input has exactly two comma-separated parts and the second
    part is a bare two-letter US state abbreviation (e.g. "San Francisco, CA"),
    we return ``'US'`` immediately instead of letting the abbreviation
    fall through to the generic country-code lookup.  This prevents
    CA → Canada, IN → India, DE → Germany and similar misclassifications.

    Parameters
    ----------
    raw : str | None
        Free-text location (e.g. ``'Bangalore, India'``, ``'London, UK'``).

    Returns
    -------
    NormResult
        ``.value`` is a two-letter country code (``'IN'``, ``'GB'``, …) or
        ``None`` if resolution failed.
    """
    if not raw or not raw.strip():
        return NormResult(value=None, normalized=True, method="country_empty")

    tokens = [t.strip() for t in raw.split(",") if t.strip()]

    # US-state guard: "City, ST" pattern where ST is a US state abbreviation.
    # This MUST run before the generic pycountry lookup to avoid collisions
    # like CA→Canada, IN→India, DE→Germany.
    if len(tokens) == 2:
        maybe_state = tokens[1].strip().upper()
        if maybe_state in _US_STATE_ABBREVS:
            return NormResult(
                value="US",
                normalized=True,
                method="country_us_state",
            )

    # Walk tokens in reverse — country name is usually the last element.
    for token in reversed(tokens):
        result = _lookup_country(token)
        if result is not None:
            return result

    # Forward pass (catches single-token inputs that failed above).
    for token in tokens:
        result = _lookup_country(token)
        if result is not None:
            return result

    return NormResult(value=None, normalized=False, method="country_unresolved")


# Common aliases that pycountry's fuzzy search resolves incorrectly
# (e.g. "UK" → Uganda).  Checked deterministically before any fuzzy pass.
_COUNTRY_ALIASES: dict[str, str] = {
    "uk": "GB",
    "u.k.": "GB",
    "england": "GB",
    "scotland": "GB",
    "wales": "GB",
    "britain": "GB",
    "great britain": "GB",
    "usa": "US",
    "u.s.a.": "US",
    "u.s.": "US",
    "united states of america": "US",
    "america": "US",
    "south korea": "KR",
    "korea": "KR",
    "north korea": "KP",
    "russia": "RU",
    "taiwan": "TW",
    "czech republic": "CZ",
    "czechia": "CZ",
    "uae": "AE",
    "holland": "NL",
    "the netherlands": "NL",
}


def _lookup_country(token: str) -> NormResult | None:
    """Try alias map, then exact lookup, then fuzzy search for a single token.

    Returns a ``NormResult`` on success, ``None`` on failure.
    """
    normalised_token = token.strip().lower()

    # 1. Deterministic alias map (handles UK, USA, etc.)
    if normalised_token in _COUNTRY_ALIASES:
        return NormResult(
            value=_COUNTRY_ALIASES[normalised_token],
            normalized=True,
            method="country_alias",
        )

    # 2. Exact / code lookup via pycountry
    try:
        country = pycountry.countries.lookup(token)
        return NormResult(
            value=country.alpha_2,
            normalized=True,
            method="country_lookup",
        )
    except LookupError:
        pass

    # 3. Fuzzy search (pycountry's built-in)
    try:
        results = pycountry.countries.search_fuzzy(token)
        if results:
            return NormResult(
                value=results[0].alpha_2,
                normalized=True,
                method="country_fuzzy",
            )
    except LookupError:
        pass

    return None


# ─────────────────────────────────────────────────────────────────────────────
# 5. Skill normaliser (taxonomy fuzzy-match via rapidfuzz)
# ─────────────────────────────────────────────────────────────────────────────

def _load_taxonomy() -> list[str]:
    """Load and cache the canonical skills taxonomy from disk.

    The taxonomy is read once and held in a module-level singleton for the
    lifetime of the process.
    """
    global _taxonomy_cache
    if _taxonomy_cache is None:
        with open(_TAXONOMY_PATH, encoding="utf-8") as fh:
            data = json.load(fh)
        if not isinstance(data, list):
            raise TypeError(
                f"skills_taxonomy.json must be a JSON array, got {type(data).__name__}"
            )
        _taxonomy_cache = data
    return _taxonomy_cache


def normalize_skill(raw: str) -> NormResult:
    """Match an incoming skill string against the canonical taxonomy.

    Uses ``rapidfuzz.fuzz.token_sort_ratio`` for comparison.

    * Score **≥ 87** → return the canonical taxonomy entry.
    * Score **< 87** → return the raw input with a ``-0.15`` penalty.

    Parameters
    ----------
    raw : str
        The skill string as extracted from the source.

    Returns
    -------
    NormResult
        ``.value`` is the canonical name or the original *raw* input.
        ``.penalty`` is ``-0.15`` when the skill could not be matched.
    """
    if not raw or not raw.strip():
        return NormResult(
            value=raw,
            normalized=False,
            penalty=0.0,
            method="skill_empty",
        )

    text = raw.strip()
    taxonomy = _load_taxonomy()

    best_score: float = 0.0
    best_match: str = ""

    for canonical in taxonomy:
        score = fuzz.token_sort_ratio(text.lower(), canonical.lower())
        if score > best_score:
            best_score = score
            best_match = canonical

    if best_score >= 87:
        penalty = 0.0 if best_score == 100 else -0.10
        return NormResult(
            value=best_match,
            normalized=True,
            penalty=penalty,
            method="skill_taxonomy_match",
        )

    return NormResult(
        value=text,
        normalized=False,
        penalty=0.0,
        method="skill_fuzzy_miss",
    )
