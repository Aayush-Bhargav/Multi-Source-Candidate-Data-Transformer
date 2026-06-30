"""
pipeline.py — Generator-based orchestrator for the candidate data pipeline.

Streams raw source files record-by-record into lightweight fragment
dictionaries, maintaining a flat, bounded memory footprint regardless of
candidate volume.

Architecture
────────────
1. **Source manifest**: a list of ``(adapter_callable, path_or_endpoint)``
   tuples describing every data source to ingest.
2. **Fragment streaming**: each adapter is a generator that yields
   ``CandidateFragment`` instances one at a time — no full-file
   materialisation.
3. **Accumulation**: fragments are immediately flattened to plain dicts
   (via ``model_dump()``) and appended to an in-memory list.  This keeps
   per-record memory cost constant and avoids holding heavy Pydantic model
   trees.
4. The accumulated fragment dicts are the input to the downstream matching /
   merging phase (not implemented in this module).

GitHub enrichment precondition
──────────────────────────────
To link a GitHub fragment to the correct candidate group during entity
resolution, the orchestrator (CLI / caller) must pass a ``candidate_hint``
dict via ``adapter_kwargs``::

    adapter_kwargs = {
        "github_adapter": {
            "candidate_hint": {"email": "alice@example.com"}
        }
    }

Without this hint, GitHub fragments will form isolated groups since the
API rarely exposes a public email or phone number.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Callable, Generator

from src.schema import CandidateFragment

logger = logging.getLogger(__name__)

# Type alias for an adapter: a callable that accepts a single path/endpoint
# string and yields CandidateFragment instances.
AdapterFn = Callable[..., Generator[CandidateFragment, None, None]]


# ─────────────────────────────────────────────────────────────────────────────
# Fragment streaming core
# ─────────────────────────────────────────────────────────────────────────────

def stream_fragments(
    sources: list[tuple[AdapterFn, str | Path]],
    *,
    adapter_kwargs: dict[str, dict[str, Any]] | None = None,
) -> Generator[dict[str, Any], None, None]:
    """Yield lightweight fragment dicts from an ordered list of data sources.

    Each source is processed lazily — the adapter generator is consumed
    record-by-record so that only one ``CandidateFragment`` is alive at any
    moment per source.  The Pydantic model is immediately converted to a
    plain dict via ``.model_dump()`` to keep the memory footprint flat.

    Parameters
    ----------
    sources : list[tuple[AdapterFn, str | Path]]
        Each element is ``(adapter_function, path_or_endpoint)``.
    adapter_kwargs : dict[str, dict[str, Any]] | None
        Optional per-adapter keyword arguments keyed by adapter function
        name (e.g. ``{"github_adapter": {"mock_file": "..."}``).

    Yields
    ------
    dict[str, Any]
        A plain dictionary produced by ``CandidateFragment.model_dump()``.
    """
    adapter_kwargs = adapter_kwargs or {}

    for adapter_fn, source_path in sources:
        fn_name = getattr(adapter_fn, "__name__", repr(adapter_fn))
        logger.info("pipeline: streaming from %s — %s", fn_name, source_path)

        extra = adapter_kwargs.get(fn_name, {})

        try:
            for fragment in adapter_fn(source_path, **extra):
                # Immediately flatten to dict — drop the Pydantic model
                yield fragment.model_dump()
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "pipeline: adapter %s raised unexpectedly — %s",
                fn_name, exc,
            )
            # Yield a failed sentinel so downstream counters stay accurate
            yield CandidateFragment(
                source_identifier=f"failed:{fn_name}",
                raw_extracted_id=str(exc),
            ).model_dump()


def accumulate_fragments(
    sources: list[tuple[AdapterFn, str | Path]],
    *,
    adapter_kwargs: dict[str, dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Materialise all fragment dicts into a flat in-memory list.

    This is the primary entry-point for the matching phase: it fully
    consumes :func:`stream_fragments` and returns the accumulated list.
    Memory cost is O(n) in the number of *fragments* (each a small dict),
    **not** in the size of the raw source files.

    Parameters
    ----------
    sources : list[tuple[AdapterFn, str | Path]]
        See :func:`stream_fragments`.
    adapter_kwargs : dict[str, dict[str, Any]] | None
        See :func:`stream_fragments`.

    Returns
    -------
    list[dict[str, Any]]
        Lightweight fragment dictionaries ready for the merge / dedup phase.
    """
    fragments: list[dict[str, Any]] = []
    failed_count = 0
    total_count = 0

    for frag_dict in stream_fragments(
        sources, adapter_kwargs=adapter_kwargs,
    ):
        total_count += 1
        src = frag_dict.get("source_identifier", "")
        if isinstance(src, str) and src.startswith("failed:"):
            failed_count += 1
        fragments.append(frag_dict)

    logger.info(
        "pipeline: accumulated %d fragments (%d failed)",
        total_count, failed_count,
    )
    return fragments
