"""
schema.py — Immutable domain models for the Multi-Source Candidate Data Transformer.

This module defines the two core data contracts of the pipeline:

  • CandidateFragment : maximally-tolerant ingestion envelope (all fields optional).
  • CanonicalProfile  : strict, schema-compliant output contract.

Design invariants
─────────────────
1. ZERO business-logic mutation lives here.  No date parsing, no field
   synthesis, no dynamic computation.  Validators only *assert* structural
   invariants; they never transform data.
2. Round-trip serialisation safety: model_dump() → model_validate() must be
   lossless for every valid instance.
3. Pydantic v2 (pydantic >= 2.0) is the sole runtime dependency.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field, model_validator


# ─────────────────────────────────────────────────────────────────────────────
# Ingestion model — every field is optional for maximum source tolerance
# ─────────────────────────────────────────────────────────────────────────────

class CandidateFragment(BaseModel):
    """A flat, transient envelope that captures raw data from any single source.

    Every field is optional so that partial / malformed extractions can still
    be ingested without raising validation errors at the boundary.
    """

    source_identifier: str | None = None
    raw_extracted_id: str | None = None
    full_name: str | None = None
    emails: list[str] = Field(default_factory=list)
    phones: list[str] = Field(default_factory=list)
    location_raw: str | None = None
    headline: str | None = None
    skills_raw: list[str] = Field(default_factory=list)
    experience_raw: list[dict[str, Any]] = Field(default_factory=list)
    education_raw: list[dict[str, Any]] = Field(default_factory=list)
    links_raw: dict[str, Any] | None = None
    id_is_synthetic: bool = False


# ─────────────────────────────────────────────────────────────────────────────
# Canonical output model — the strict domain contract
# ─────────────────────────────────────────────────────────────────────────────

# Required key-sets for structured list elements.  Used by the model validator
# to assert shape invariants without mutating data.
_SKILL_KEYS: frozenset[str] = frozenset({"name", "confidence", "sources"})
_EXPERIENCE_KEYS: frozenset[str] = frozenset(
    {"company", "title", "start", "end", "summary"},
)
_EDUCATION_KEYS: frozenset[str] = frozenset(
    {"institution", "degree", "field", "end_year"},
)
_PROVENANCE_KEYS: frozenset[str] = frozenset({"field", "source", "method"})
_LOCATION_KEYS: frozenset[str] = frozenset({"city", "region", "country"})
_LINKS_KEYS: frozenset[str] = frozenset(
    {"linkedin", "github", "portfolio", "other"},
)


class CanonicalProfile(BaseModel):
    """Schema-compliant domain contract for a fully-resolved candidate profile.

    Every field is required (or explicitly typed as ``None``-able) so that
    downstream consumers can rely on a deterministic shape.  Structural
    validators enforce key-presence and value-range invariants but perform
    **no** data mutation.
    """

    candidate_id: str
    full_name: str
    emails: list[str] = Field(default_factory=list)
    phones: list[str] = Field(default_factory=list)
    location: dict[str, Any] = Field(
        default_factory=lambda: {"city": None, "region": None, "country": None},
    )
    links: dict[str, Any] = Field(
        default_factory=lambda: {
            "linkedin": None,
            "github": None,
            "portfolio": None,
            "other": [],
        },
    )
    headline: str | None = None
    years_experience: float | None = None
    skills: list[dict[str, Any]] = Field(default_factory=list)
    experience: list[dict[str, Any]] = Field(default_factory=list)
    education: list[dict[str, Any]] = Field(default_factory=list)
    overall_confidence: float = 0.0
    provenance: list[dict[str, Any]] = Field(default_factory=list)

    # Visible, non-underscore metadata carrier — excluded from serialisation
    # so it never leaks into JSON output but remains accessible in-memory.
    field_metadata: dict[str, Any] = Field(default_factory=dict, exclude=True)

    # ── Structural invariant validators (assert-only, no mutation) ────────

    @model_validator(mode="after")
    def _assert_invariants(self) -> "CanonicalProfile":
        """Assert every structural invariant in a single pass.

        Raises ``ValueError`` on any violation.  Never mutates ``self``.
        """
        # -- overall_confidence range --
        if not 0.0 <= self.overall_confidence <= 1.0:
            raise ValueError(
                f"overall_confidence must be in [0.0, 1.0], "
                f"got {self.overall_confidence!r}"
            )

        # -- years_experience non-negative when present --
        if self.years_experience is not None and self.years_experience < 0.0:
            raise ValueError(
                f"years_experience must be >= 0.0 when set, "
                f"got {self.years_experience!r}"
            )

        # -- location key structure --
        _assert_dict_keys(
            self.location, _LOCATION_KEYS, label="location", exact=True
        )

        # -- links key structure --
        _assert_dict_keys(self.links, _LINKS_KEYS, label="links", exact=True)
        if not isinstance(self.links.get("other"), list):
            raise ValueError(
                "links.other must be a list"
            )

        # -- skills element structure --
        for idx, entry in enumerate(self.skills):
            _assert_dict_keys(entry, _SKILL_KEYS, label=f"skills[{idx}]")
            if not isinstance(entry.get("sources"), list):
                raise ValueError(
                    f"skills[{idx}].sources must be a list"
                )

        # -- experience element structure --
        for idx, entry in enumerate(self.experience):
            _assert_dict_keys(
                entry, _EXPERIENCE_KEYS, label=f"experience[{idx}]"
            )

        # -- education element structure --
        for idx, entry in enumerate(self.education):
            _assert_dict_keys(
                entry, _EDUCATION_KEYS, label=f"education[{idx}]"
            )

        # -- provenance element structure --
        for idx, entry in enumerate(self.provenance):
            _assert_dict_keys(
                entry, _PROVENANCE_KEYS, label=f"provenance[{idx}]"
            )
            if not isinstance(entry.get("source"), list):
                raise ValueError(
                    f"provenance[{idx}].source must be a list"
                )

        return self


# ─────────────────────────────────────────────────────────────────────────────
# Internal assertion helpers (pure, side-effect-free)
# ─────────────────────────────────────────────────────────────────────────────

def _assert_dict_keys(
    d: dict[str, Any],
    required: frozenset[str],
    *,
    label: str,
    exact: bool = False,
) -> None:
    """Raise ``ValueError`` if *d* is missing any key in *required*,
    or if *exact* is True and *d* contains extra keys.

    This is an assertion-only helper — it never mutates *d*.
    """
    missing = required - d.keys()
    if missing:
        raise ValueError(
            f"{label} is missing required key(s): {sorted(missing)}"
        )
    if exact:
        extra = d.keys() - required
        if extra:
            raise ValueError(
                f"{label} contains unexpected extra key(s): {sorted(extra)}"
            )
