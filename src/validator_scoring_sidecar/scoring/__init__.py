"""Vendored foundation scoring code used by the sidecar verification path.

The sidecar verifies a foundation scoring round by parsing the same raw model
response with the same parser and applying the same UNL selector to the
resulting scores. Any drift between sidecar and foundation behavior at this
layer would produce false output-divergence claims for rounds that are in
agreement, so this package vendors the foundation parser and selector at a
pinned commit rather than re-implementing them.

Local adaptations are limited to making the modules self-contained:

- The selector's ``cutoff``, ``max_size``, and ``min_gap`` parameters are
  required and supplied by the caller from the round's execution manifest at
  ``code.selector.parameters``.
- The parser consumes its validator identity mapping directly from a dict
  built from the frozen ``inputs/validator_map.json`` file in the round's
  input package, instead of the foundation's ``PromptBuilder`` output.

Parser and selector are vendored as a unit. The foundation publishes a single
``code.commit`` per round, so any upstream update that touches one of these
files counts as an update to both for refresh purposes; do not vendor one
without re-checking the other against the same upstream commit.

The vendor files intentionally do not use ``from __future__ import
annotations`` even though the rest of this package does. That annotation
import would change pydantic's runtime annotation resolution and break
behavioral parity with the foundation modules; the asymmetry is deliberate to
preserve byte-fidelity with the upstream.

Refresh procedure when the foundation updates parser or selector:

1. Identify the new foundation commit on ``postfiatorg/dynamic-unl-scoring``
   that updated ``scoring_service/services/response_parser.py`` or
   ``scoring_service/services/unl_selector.py``.
2. Copy the updated file(s) over ``parser.py`` / ``selector.py`` in this
   package.
3. Re-apply the local adaptations above so the modules remain self-contained.
4. Add the new commit identifier to ``SCORING_CODE_VERSION`` as an additional
   supported version. Do not remove the prior identifier yet.
5. Keep the prior identifier in ``SCORING_CODE_VERSION`` until every devnet
   and testnet round produced by the prior foundation commit has been
   verified by the sidecar.
6. Only then drop the prior identifier from ``SCORING_CODE_VERSION``.

``SCORING_CODE_VERSION`` is a frozenset of ``"git:<commit>"`` identifiers
matching the format the foundation emits in the execution manifest's
``code.parser.version`` and ``code.selector.version`` fields. The manifest
compatibility checker fails closed when a round's parser or selector version
falls outside this set.
"""

from validator_scoring_sidecar.scoring.parser import (
    DIMENSIONAL_FIELDS,
    NetworkReport,
    NetworkReportCategory,
    ScoringResult,
    ValidatorScore,
    parse_response,
)
from validator_scoring_sidecar.scoring.selector import (
    UNLSelectionResult,
    select_unl,
)

SCORING_CODE_VERSION: frozenset[str] = frozenset(
    {
        "git:43bc6946d6991d0bbf7a1b75a7e326a7ab52411b",
    }
)

__all__ = [
    "DIMENSIONAL_FIELDS",
    "NetworkReport",
    "NetworkReportCategory",
    "SCORING_CODE_VERSION",
    "ScoringResult",
    "UNLSelectionResult",
    "ValidatorScore",
    "parse_response",
    "select_unl",
]
