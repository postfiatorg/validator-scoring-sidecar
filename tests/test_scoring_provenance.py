"""Provenance tests for the vendored scoring sub-package.

These tests prove that the values of ``SUPPORTED_PARSER_CONTENT_HASHES``,
``SUPPORTED_SELECTOR_CONTENT_HASHES``, and
``SUPPORTED_COMMIT_REVEAL_CONTENT_HASHES`` can be reconstructed from the
unadapted foundation source files checked into ``scoring/_vendor_source``. If a
constant drifts from its on-disk source, or a source file is modified without
updating the constant, the relevant test fails.

This is an internal-consistency check only. It confirms that the declared
constants match the bytes on disk in ``_vendor_source``; it does not verify
that either matches what the foundation currently publishes. That
external-truth check is the responsibility of the
``.github/workflows/vendor-freshness.yml`` CI workflow, which fetches the
foundation source at the matching branch and compares against the same
supported sets.
"""

import hashlib
from importlib.resources import files

from validator_scoring_sidecar.scoring import (
    SUPPORTED_COMMIT_REVEAL_CONTENT_HASHES,
    SUPPORTED_PARSER_CONTENT_HASHES,
    SUPPORTED_SELECTOR_CONTENT_HASHES,
)

VENDOR_SOURCE_PACKAGE = "validator_scoring_sidecar.scoring._vendor_source"


def _vendor_source_hash(filename: str) -> str:
    content = files(VENDOR_SOURCE_PACKAGE).joinpath(filename).read_bytes()
    return hashlib.sha256(content).hexdigest()


def test_parser_vendor_source_hash_is_in_supported_set():
    digest = _vendor_source_hash("response_parser.py")
    assert digest in SUPPORTED_PARSER_CONTENT_HASHES, (
        f"Vendor source response_parser.py sha256 {digest} is not in "
        f"SUPPORTED_PARSER_CONTENT_HASHES {sorted(SUPPORTED_PARSER_CONTENT_HASHES)}. "
        f"Either the source file in _vendor_source drifted from the declared "
        f"hash, or the constant needs updating per the refresh procedure in "
        f"scoring/__init__.py."
    )


def test_selector_vendor_source_hash_is_in_supported_set():
    digest = _vendor_source_hash("unl_selector.py")
    assert digest in SUPPORTED_SELECTOR_CONTENT_HASHES, (
        f"Vendor source unl_selector.py sha256 {digest} is not in "
        f"SUPPORTED_SELECTOR_CONTENT_HASHES {sorted(SUPPORTED_SELECTOR_CONTENT_HASHES)}. "
        f"Either the source file in _vendor_source drifted from the declared "
        f"hash, or the constant needs updating per the refresh procedure in "
        f"scoring/__init__.py."
    )


def test_commit_reveal_vendor_source_hash_is_in_supported_set():
    digest = _vendor_source_hash("commit_reveal.py")
    assert digest in SUPPORTED_COMMIT_REVEAL_CONTENT_HASHES, (
        f"Vendor source commit_reveal.py sha256 {digest} is not in "
        f"SUPPORTED_COMMIT_REVEAL_CONTENT_HASHES "
        f"{sorted(SUPPORTED_COMMIT_REVEAL_CONTENT_HASHES)}. "
        f"Either the source file in _vendor_source drifted from the declared "
        f"hash, or the constant needs updating per the refresh procedure in "
        f"scoring/__init__.py."
    )


def test_supported_hash_sets_are_non_empty():
    assert SUPPORTED_PARSER_CONTENT_HASHES, (
        "SUPPORTED_PARSER_CONTENT_HASHES must declare at least one supported hash"
    )
    assert SUPPORTED_SELECTOR_CONTENT_HASHES, (
        "SUPPORTED_SELECTOR_CONTENT_HASHES must declare at least one supported hash"
    )
    assert SUPPORTED_COMMIT_REVEAL_CONTENT_HASHES, (
        "SUPPORTED_COMMIT_REVEAL_CONTENT_HASHES must declare at least one supported hash"
    )
