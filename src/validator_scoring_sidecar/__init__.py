"""Validator scoring sidecar package."""

from importlib.metadata import PackageNotFoundError, version

try:
    # Resolve from installed package metadata so pyproject.toml stays the single
    # source of truth for the release version. Operators always run an installed
    # package (the published image installs it), so this is the normal path.
    __version__ = version("validator-scoring-sidecar")
except PackageNotFoundError:  # pragma: no cover - source tree without an install
    __version__ = "0.0.0+unknown"
