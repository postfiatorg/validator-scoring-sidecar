"""Shared failure-category vocabulary used by the sidecar verification path.

Every stage of the Dynamic UNL sidecar verification — manifest compatibility
checking, inference execution, output normalization, and convergence
reporting — emits structured failures using the same enum so the foundation
and the sidecar speak one vocabulary. This module is the single source of
truth for that vocabulary and the dataclass that carries the surrounding
context for each failure instance.

The compatibility checker in ``manifest.py`` only emits a subset of these
categories (``MANIFEST_UNSUPPORTED``, ``MANIFEST_INCOMPATIBLE``,
``SKIPPED_OVERRIDE``, and ``SKIPPED_OPERATOR_OPT_OUT``). The rest are
reserved for later milestones: inference backends emit ``RUNTIME_UNAVAILABLE``
and the ``INFERENCE_*`` family; output normalization emits ``PARSER_ERROR``,
``SELECTOR_ERROR``, and ``OUTPUT_DIVERGENCE``; chain integration emits
``REVEAL_WINDOW_MISSED``.
"""

from __future__ import annotations

from dataclasses import dataclass, field as dataclass_field
from enum import Enum
from typing import Any


class FailureCategory(str, Enum):
    """Categories the sidecar may emit when a round cannot be verified."""

    MANIFEST_UNSUPPORTED = "MANIFEST_UNSUPPORTED"
    MANIFEST_INCOMPATIBLE = "MANIFEST_INCOMPATIBLE"
    RUNTIME_UNAVAILABLE = "RUNTIME_UNAVAILABLE"
    INFERENCE_TIMEOUT = "INFERENCE_TIMEOUT"
    INFERENCE_ERROR = "INFERENCE_ERROR"
    PARSER_ERROR = "PARSER_ERROR"
    SELECTOR_ERROR = "SELECTOR_ERROR"
    OUTPUT_DIVERGENCE = "OUTPUT_DIVERGENCE"
    SKIPPED_OVERRIDE = "SKIPPED_OVERRIDE"
    SKIPPED_OPERATOR_OPT_OUT = "SKIPPED_OPERATOR_OPT_OUT"
    REVEAL_WINDOW_MISSED = "REVEAL_WINDOW_MISSED"


@dataclass(frozen=True)
class Failure:
    """Structured failure record emitted by sidecar verification stages.

    ``field`` names the offending manifest or deployment-record field when
    relevant (e.g. for ``MANIFEST_INCOMPATIBLE``). ``message`` is a
    human-readable explanation. ``details`` carries additional structured
    context that future convergence reporting (M2.6) consumes.
    """

    category: FailureCategory
    # Attribute name fixed by the failure taxonomy in SidecarScoringSpec.md;
    # do not rename without a spec change.
    field: str | None = None
    message: str | None = None
    details: dict[str, Any] = dataclass_field(default_factory=dict)
