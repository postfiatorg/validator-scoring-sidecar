"""Vendored foundation scoring code used by the sidecar verification path.

The sidecar verifies a foundation scoring round by parsing the same raw model
response with the same parser and applying the same UNL selector to the
resulting scores. Any drift between sidecar and foundation behavior at this
layer would produce false output-divergence claims for rounds that are in
agreement, so this package vendors the foundation parser and selector at a
pinned commit rather than re-implementing them.

The same package also vendors the foundation's ``commit_reveal.py`` protocol
module, which the chain-integration path uses to decode and validate round
announcements (and later to build and verify commit/reveal payloads). Unlike
the parser and selector it imports only the standard library and ``xrpl.core``
with no foundation-internal dependencies, so it needs no local adaptation: the
byte-identical copy in ``_vendor_source`` is re-exported here and imported
directly at runtime, guarded by ``SUPPORTED_COMMIT_REVEAL_CONTENT_HASHES`` and
the same freshness CI and provenance test as the parser and selector.

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

Foundation-equivalence identification
-------------------------------------

The foundation publishes ``code.parser.version`` and ``code.selector.version``
in each round's execution manifest as the whole-repo deploy commit hash, not
the commit where those specific files last changed. Identifying foundation
equivalence by commit hash would refuse legitimate rounds whenever the
foundation deploys any unrelated change. The sidecar therefore identifies
foundation equivalence by the sha256 content hash of the foundation source
files instead.

``SUPPORTED_PARSER_CONTENT_HASHES`` and ``SUPPORTED_SELECTOR_CONTENT_HASHES``
are the sha256 digests of the foundation's
``scoring_service/services/response_parser.py`` and
``scoring_service/services/unl_selector.py`` files at the commits the sidecar
vendor was lifted from. The unadapted source files are checked into the
``_vendor_source`` directory inside this package so the declared hashes are
auditable: anyone can recompute the digests from disk and confirm they match
the declared constants (``tests/test_scoring_provenance.py`` enforces this).

Refresh procedure when the foundation updates parser or selector:

1. Fetch the new foundation ``response_parser.py`` and ``unl_selector.py``
   from ``postfiatorg/dynamic-unl-scoring`` at the deployed branch
   (``main``, ``devnet``, or ``testnet``).
2. Compute sha256 of each file (mechanical; the CI workflow at
   ``.github/workflows/vendor-freshness.yml`` does this automatically).
3. If the new hash is already in the corresponding supported set, the
   foundation did not actually change that file; no action.
4. If a hash is new, diff the new file against the existing copy in
   ``_vendor_source`` (human judgment). Two outcomes:

   - **Cosmetic** (comments, whitespace, refactor that preserves behavior):
     replace the file in ``_vendor_source`` with the new content, re-apply
     the local adaptations to ``parser.py`` / ``selector.py``, and add the
     new hash to the supported set. Run the test suite to confirm behavior
     is preserved.
   - **Behavioral** (new field, new validation, changed control flow):
     vendor refresh required. Copy the new file into ``_vendor_source``,
     re-apply the local adaptations, and add the new hash to the supported
     set. Keep the prior hash in the set until both devnet and testnet have
     deployed the new foundation commit AND at least one round on each has
     been successfully verified by a sidecar running the new vendor with no
     manifest-incompatible errors. Only then drop the prior hash.

For ``commit_reveal.py`` the procedure is the same but simpler: it has no local
adaptation and no separate runnable copy, so a refresh is just replacing the
file in ``_vendor_source`` and adding the new digest to
``SUPPORTED_COMMIT_REVEAL_CONTENT_HASHES``.

The CI workflow ``.github/workflows/vendor-freshness.yml`` runs the
mechanical part of this procedure on every push and pull request against
``main``, ``devnet``, and ``testnet``. Drift on ``main`` is reported but
non-blocking so day-to-day development is not gated on immediate vendor
refresh. Drift on ``devnet`` or ``testnet`` fails the workflow because those
branches map directly to deployed sidecar environments and must remain
synchronized with the corresponding deployed foundation branch.
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
from validator_scoring_sidecar.scoring._vendor_source import commit_reveal

SUPPORTED_PARSER_CONTENT_HASHES: frozenset[str] = frozenset(
    {
        "1eeeed7bee91d2e6e95039018074c5e30ba3e92dffaa16257e6e5dbd07a2f7f7",
    }
)
SUPPORTED_SELECTOR_CONTENT_HASHES: frozenset[str] = frozenset(
    {
        "cdd65a60565ba5ac340b5be60421f770905fc461cefa770c71465a179c2ff9f2",
    }
)
SUPPORTED_COMMIT_REVEAL_CONTENT_HASHES: frozenset[str] = frozenset(
    {
        "5ce025098523557a2d02f828e00bfa1e82ddc6323cff5af3f9f8a4bc04c65049",
    }
)

__all__ = [
    "DIMENSIONAL_FIELDS",
    "NetworkReport",
    "NetworkReportCategory",
    "SUPPORTED_COMMIT_REVEAL_CONTENT_HASHES",
    "SUPPORTED_PARSER_CONTENT_HASHES",
    "SUPPORTED_SELECTOR_CONTENT_HASHES",
    "ScoringResult",
    "UNLSelectionResult",
    "ValidatorScore",
    "commit_reveal",
    "parse_response",
    "select_unl",
]
