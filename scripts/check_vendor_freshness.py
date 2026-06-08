"""Drift detection between sidecar vendor and foundation parser/selector/commit-reveal.

Fetches ``scoring_service/services/response_parser.py``,
``scoring_service/services/unl_selector.py``, and
``scoring_service/services/commit_reveal.py`` from
``postfiatorg/dynamic-unl-scoring`` at the given branch, computes sha256 over
each, and compares against the sidecar's ``SUPPORTED_PARSER_CONTENT_HASHES``,
``SUPPORTED_SELECTOR_CONTENT_HASHES``, and
``SUPPORTED_COMMIT_REVEAL_CONTENT_HASHES``.

Exit codes:

- 0: both hashes are in the supported sets, or drift was detected but
  ``--mode warning`` was passed.
- 1: drift was detected and ``--mode blocking`` was passed.
- 2: a network or unexpected runtime error occurred.

Used by the ``vendor-freshness`` GitHub Actions workflow; also runnable
locally for ad-hoc checks.
"""

import argparse
import hashlib
import sys
import urllib.error
import urllib.request

from validator_scoring_sidecar.scoring import (
    SUPPORTED_COMMIT_REVEAL_CONTENT_HASHES,
    SUPPORTED_PARSER_CONTENT_HASHES,
    SUPPORTED_SELECTOR_CONTENT_HASHES,
)

FOUNDATION_RAW_BASE = (
    "https://raw.githubusercontent.com/postfiatorg/dynamic-unl-scoring"
)
PARSER_PATH = "scoring_service/services/response_parser.py"
SELECTOR_PATH = "scoring_service/services/unl_selector.py"
COMMIT_REVEAL_PATH = "scoring_service/services/commit_reveal.py"
HTTP_TIMEOUT_SECONDS = 30
HTTP_NOT_FOUND = 404
EXIT_OK = 0
EXIT_DRIFT = 1
EXIT_ERROR = 2
DESCRIPTION = "Drift detection between sidecar vendor and foundation parser/selector/commit-reveal."


def _fetch(branch: str, path: str) -> bytes:
    url = f"{FOUNDATION_RAW_BASE}/{branch}/{path}"
    with urllib.request.urlopen(url, timeout=HTTP_TIMEOUT_SECONDS) as response:
        return response.read()


def _check_module(
    branch: str,
    module_label: str,
    path: str,
    supported: frozenset[str],
) -> bool:
    try:
        content = _fetch(branch, path)
    except urllib.error.HTTPError as exc:
        if exc.code == HTTP_NOT_FOUND:
            print(
                f"DRIFT: foundation {module_label} ({path}) "
                f"not found at branch '{branch}' (HTTP {exc.code}); "
                f"foundation may have renamed or moved this file"
            )
            return False
        raise

    digest = hashlib.sha256(content).hexdigest()
    matched = digest in supported
    if matched:
        print(
            f"OK: foundation {module_label} ({path}) "
            f"sha256 {digest} is in supported set"
        )
    else:
        print(
            f"DRIFT: foundation {module_label} ({path}) "
            f"sha256 {digest} is not in supported set "
            f"{sorted(supported)}"
        )
    return matched


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=DESCRIPTION)
    parser.add_argument(
        "--branch",
        required=True,
        help="dynamic-unl-scoring branch to fetch (main, devnet, testnet)",
    )
    parser.add_argument(
        "--mode",
        choices=("blocking", "warning"),
        default="blocking",
        help=(
            "blocking: exit nonzero on drift. "
            "warning: report drift but always exit zero."
        ),
    )
    args = parser.parse_args(argv)

    try:
        parser_matched = _check_module(
            args.branch,
            "parser",
            PARSER_PATH,
            SUPPORTED_PARSER_CONTENT_HASHES,
        )
        selector_matched = _check_module(
            args.branch,
            "selector",
            SELECTOR_PATH,
            SUPPORTED_SELECTOR_CONTENT_HASHES,
        )
        commit_reveal_matched = _check_module(
            args.branch,
            "commit-reveal",
            COMMIT_REVEAL_PATH,
            SUPPORTED_COMMIT_REVEAL_CONTENT_HASHES,
        )
    except urllib.error.URLError as exc:
        print(f"ERROR: failed to fetch foundation source: {exc}", file=sys.stderr)
        return EXIT_ERROR

    if parser_matched and selector_matched and commit_reveal_matched:
        return EXIT_OK

    print()
    print(
        f"Drift detected against postfiatorg/dynamic-unl-scoring "
        f"branch '{args.branch}'."
    )
    print(
        "Maintainer action: either the foundation made a behavioral change "
        "and the sidecar vendor needs a refresh, or the change is cosmetic "
        "and the new hash can be added to the supported set after a manual "
        "behavioral diff. See the refresh procedure in "
        "src/validator_scoring_sidecar/scoring/__init__.py."
    )

    if args.mode == "warning":
        print("Mode is 'warning'; exiting 0 despite drift.")
        return EXIT_OK
    return EXIT_DRIFT


if __name__ == "__main__":
    sys.exit(main())
