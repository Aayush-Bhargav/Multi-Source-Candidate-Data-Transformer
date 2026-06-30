"""
project.py — Configurable runtime projection engine (Eightfold directives).

Reshapes a validated ``CanonicalProfile`` into a custom output dictionary
according to a JSON configuration.  Implements five core directives:

1. **Field Selection & Key Remapping** — dot/bracket path resolution.
2. **Per-Field Normalization Override** — re-routes extracted values through
   ``src.normalize`` when specified (e.g. ``"normalize": "E164"``).
3. **Provenance / Confidence Injection** — attaches a ``_metadata`` block
   mirroring the projected shape with confidence, source, and method.
4. **On-Missing Handling** — ``null``, ``omit``, or ``error`` strategies.
5. **Contradiction Guard** — uses ``pydantic.create_model`` to dynamically
   validate the projected payload, catching required-vs-omit conflicts.
"""
from __future__ import annotations

import logging
import re
from typing import Any, Optional

from pydantic import ValidationError, create_model

from src.normalize import normalize_phone, normalize_skill
from src.schema import CanonicalProfile

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
# Custom exception
# ─────────────────────────────────────────────────────────────────────────────

class MissingFieldError(Exception):
    """Raised when an on_missing='error' field cannot be resolved."""


# ─────────────────────────────────────────────────────────────────────────────
# Path resolution
# ─────────────────────────────────────────────────────────────────────────────

# Matches tokens like "experience", "experience[0]", "skills[:5]"
_PATH_TOKEN_RE = re.compile(
    r"(?P<key>[A-Za-z_]\w*)"
    r"(?:\[(?P<idx>-?\d+)\])?"
    r"(?:\[:(?P<slice>\d+)\])?"
    r"(?:\[\])?"
)


def _resolve_path(data: dict[str, Any], path: str) -> Any:
    """Resolve a dot/bracket path against a nested dict/list structure.

    Supported syntax examples::

        "candidate_id"             → scalar lookup
        "emails[0]"               → first element of a list
        "experience[0].company"   → nested into list element's dict
        "location.city"           → nested dict access
        "skills[:5].name"         → slice first 5, pluck 'name' from each

    Returns ``_SENTINEL`` when resolution fails at any level.
    """
    current: Any = data
    segments = path.split(".")

    for seg in segments:
        m = _PATH_TOKEN_RE.fullmatch(seg)
        if not m:
            return _SENTINEL

        key = m.group("key")
        idx = m.group("idx")
        slc = m.group("slice")

        # Dict key lookup
        if isinstance(current, dict):
            if key not in current:
                return _SENTINEL
            current = current[key]
        elif isinstance(current, list):
            # When current is already a list (from a previous slice),
            # pluck 'key' from each element
            plucked = []
            for item in current:
                if isinstance(item, dict) and key in item:
                    plucked.append(item[key])
            current = plucked if plucked else _SENTINEL
            if current is _SENTINEL:
                return _SENTINEL
        else:
            return _SENTINEL

        # Index into list
        if idx is not None:
            idx_int = int(idx)
            if isinstance(current, list) and -len(current) <= idx_int < len(current):
                current = current[idx_int]
            else:
                return _SENTINEL

        # Slice list
        if slc is not None:
            slc_int = int(slc)
            if isinstance(current, list):
                current = current[:slc_int]
            else:
                return _SENTINEL

    return current


class _SentinelType:
    """Singleton sentinel for 'not found' — distinct from None."""
    _instance = None

    def __new__(cls) -> "_SentinelType":
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __repr__(self) -> str:
        return "<MISSING>"

    def __bool__(self) -> bool:
        return False


_SENTINEL = _SentinelType()


# ─────────────────────────────────────────────────────────────────────────────
# Normalization dispatch
# ─────────────────────────────────────────────────────────────────────────────

_NORMALIZER_DISPATCH: dict[str, Any] = {
    "e164": lambda v: normalize_phone(str(v)).value if v else v,
    "canonical": lambda v: normalize_skill(str(v)).value if v else v,
}


def _apply_normalization(value: Any, method: str) -> Any:
    """Route a value through the requested normalizer."""
    fn = _NORMALIZER_DISPATCH.get(method.lower())
    if fn is None:
        logger.warning("project: unknown normalize method '%s', passing through", method)
        return value
    if isinstance(value, list):
        return [fn(v) for v in value]
    return fn(value)


# ─────────────────────────────────────────────────────────────────────────────
# Type mapping for dynamic Pydantic model
# ─────────────────────────────────────────────────────────────────────────────

_TYPE_MAP: dict[str, type] = {
    "str": str, "string": str,
    "int": int, "number": float,
    "float": float,
    "bool": bool, "boolean": bool,
    "list": list, "string[]": list, "array": list,
    "dict": dict, "object": dict,
}


# ─────────────────────────────────────────────────────────────────────────────
# ConfigurableProjector
# ─────────────────────────────────────────────────────────────────────────────

class ConfigurableProjector:
    """Reshapes a ``CanonicalProfile`` according to a JSON configuration.

    Parameters
    ----------
    config : dict[str, Any]
        A projection config dict with a ``"fields"`` list and optional
        ``"include_confidence"`` flag.  Each field entry has:

        - ``target``: output key name
        - ``from``: dot/bracket path into the CanonicalProfile
        - ``type``: expected Python type name (str, int, float, …)
        - ``required``: whether the field is mandatory (bool)
        - ``on_missing``: one of ``"null"``, ``"omit"``, ``"error"``
        - ``normalize``: optional normalization method (e.g. ``"E164"``)
    """

    def __init__(self, config: dict[str, Any]) -> None:
        self.config = config
        
        self.field_specs: list[dict[str, Any]] = []
        for spec in config.get("fields", []):
            target = spec.get("path", spec.get("target"))
            from_path = spec.get("from", target)
            self.field_specs.append({
                **spec,
                "target": target,
                "from": from_path
            })
            
        self.include_confidence: bool = config.get("include_confidence", False)

        model_fields: dict[str, Any] = {}
        for spec in self.field_specs:
            target = spec["target"]
            field_type = _TYPE_MAP.get(str(spec.get("type", "str")).lower(), Any)
            is_required = spec.get("required", False)
            if is_required:
                model_fields[target] = (field_type, ...)
            else:
                model_fields[target] = (Optional[field_type], None)

        self.DynamicModel = create_model("ProjectedProfile", **model_fields)

    def project(self, profile: CanonicalProfile) -> dict[str, Any]:
        """Project *profile* into the configured output shape.

        Returns
        -------
        dict[str, Any]
            The reshaped output dictionary, validated against a dynamic
            Pydantic model.

        Raises
        ------
        MissingFieldError
            When an ``on_missing='error'`` field cannot be resolved.
        ValidationError
            When the Contradiction Guard detects a ``required=True`` field
            was omitted.
        """
        source = profile.model_dump()
        field_metadata = profile.field_metadata or {}
        projected: dict[str, Any] = {}
        metadata_block: dict[str, Any] = {}
        
        # Determine global on_missing policy from config, default "null"
        global_on_missing = self.config.get("on_missing", "null")

        for spec in self.field_specs:
            target = spec["target"]
            from_path = spec["from"]
            on_missing = spec.get("on_missing", global_on_missing)
            normalize_method = spec.get("normalize")

            # Resolve the value
            value = _resolve_path(source, from_path)

            if value is _SENTINEL or value is None:
                # Value not found or is None
                if on_missing == "error":
                    raise MissingFieldError(
                        f"Required field '{target}' (from='{from_path}') "
                        f"is missing from profile {profile.candidate_id}"
                    )
                elif on_missing == "omit":
                    continue  # skip entirely — Contradiction Guard will catch conflicts
                else:  # "null"
                    projected[target] = None
            else:
                # Apply optional normalization override
                if normalize_method:
                    value = _apply_normalization(value, normalize_method)
                projected[target] = value

            # Build confidence metadata for this field
            if self.include_confidence and target in projected:
                # Map from-path root to field_metadata key
                meta_key = _metadata_key(from_path)
                if meta_key and meta_key in field_metadata:
                    fm = field_metadata[meta_key]
                    metadata_block[target] = {
                        "confidence": fm.get("confidence", 0.0),
                        "sources": fm.get("sources", []),
                        "method": fm.get("method", ""),
                    }

        # ── Provenance / Confidence injection ──
        if self.include_confidence and metadata_block:
            projected["_metadata"] = metadata_block

        # ── Contradiction Guard (Dynamic Validation) ──
        self._validate_contradiction(projected)

        return projected

    def _validate_contradiction(self, projected: dict[str, Any]) -> None:
        """Use ``pydantic.create_model`` to dynamically validate the output.

        If a field is ``required=True`` in the config but was skipped by
        ``on_missing='omit'``, Pydantic's validation will fail, catching
        the contradiction loudly.
        """
        # Strip _metadata before validation (it's injected, not in the config)
        validation_payload = {
            k: v for k, v in projected.items() if k != "_metadata"
        }
        self.DynamicModel(**validation_payload)  # raises ValidationError on failure


def _metadata_key(from_path: str) -> str | None:
    """Map a projection 'from' path to the field_metadata key.

    Examples::

        "candidate_id"           → None (no metadata tracked for ID)
        "full_name"              → "full_name"
        "emails[0]"             → "emails"
        "location.city"         → "location"
        "experience[0].company" → "experience"
        "skills[:5].name"       → "skills"
        "overall_confidence"    → None

    """
    # No metadata for these
    if from_path in ("candidate_id", "overall_confidence"):
        return None
    # Take the root segment before any dot or bracket
    root = re.split(r"[.\[]", from_path, maxsplit=1)[0]
    return root
