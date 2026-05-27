"""Frozen input package fetching, verification, and local caching."""

from __future__ import annotations

import hashlib
import json
import shutil
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Literal, NoReturn

from validator_scoring_sidecar.config import SidecarConfig
from validator_scoring_sidecar.round_metadata import RoundMetadata
from validator_scoring_sidecar.scoring_client import (
    ScoringClient,
    ScoringClientError,
)

BUNDLE_FILE_PATH = "bundle.json"
PACKAGES_DIR_NAME = "packages"
SIDECAR_METADATA_FILE_PATH = ".sidecar/package.json"
SIDECAR_METADATA_VERSION = 1
SOURCE_AUTO = "auto"
SOURCE_CACHE = "cache"
SOURCE_HTTPS = "https"
SOURCE_IPFS = "ipfs"
PACKAGE_SOURCE_CHOICES = (SOURCE_AUTO, SOURCE_HTTPS, SOURCE_IPFS)

PackageSource = Literal["auto", "https", "ipfs"]
ResolvedPackageSource = Literal["cache", "https", "ipfs"]


class InputPackageError(RuntimeError):
    """Base error for frozen input package operations."""


class InputPackageDownloadError(InputPackageError):
    """Raised when the package cannot be fetched from a selected source."""


class InputPackageVerificationError(InputPackageError):
    """Raised when downloaded or cached package content fails verification."""


class InputPackageCacheError(InputPackageError):
    """Raised when verified package content cannot be cached safely."""


@dataclass(frozen=True)
class FetchedInputPackage:
    """Verified local input package result."""

    round_id: int
    round_number: int
    network: str
    input_package_cid: str
    input_package_hash: str
    input_frozen_at: str
    source: ResolvedPackageSource
    cached: bool
    local_path: Path
    verified_file_count: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "round_id": self.round_id,
            "round_number": self.round_number,
            "network": self.network,
            "input_package_cid": self.input_package_cid,
            "input_package_hash": self.input_package_hash,
            "input_frozen_at": self.input_frozen_at,
            "source": self.source,
            "cached": self.cached,
            "local_path": str(self.local_path),
            "verified_file_count": self.verified_file_count,
        }


@dataclass(frozen=True)
class _VerifiedPackageContent:
    files: dict[str, Any]
    file_hashes: dict[str, str]


def fetch_input_package(
    metadata: RoundMetadata,
    config: SidecarConfig,
    client: ScoringClient,
    *,
    source: PackageSource = SOURCE_AUTO,
    force: bool = False,
) -> FetchedInputPackage:
    """Fetch, verify, and cache one frozen input package."""

    if source not in PACKAGE_SOURCE_CHOICES:
        raise InputPackageDownloadError(f"unsupported package source: {source}")

    package_hash = _require_sha256("input_package_hash", metadata.input_package_hash)
    cache_path = package_cache_path(config, package_hash)

    if cache_path.exists() and not force:
        try:
            cached_content = verify_cached_input_package(cache_path, metadata, config)
        except InputPackageVerificationError as exc:
            raise InputPackageVerificationError(
                f"Cached input package at {cache_path} failed verification; "
                f"rerun with --force to refetch: {exc}"
            ) from exc
        return _build_result(
            metadata=metadata,
            config=config,
            cache_path=cache_path,
            source=SOURCE_CACHE,
            cached=True,
            verified_file_count=len(cached_content.file_hashes),
        )

    errors: list[tuple[str, InputPackageError]] = []
    for current_source in _source_order(source):
        try:
            content = _download_verified_content(
                metadata,
                config,
                client,
                source=current_source,
                expected_package_hash=package_hash,
            )
        except InputPackageError as exc:
            errors.append((current_source, exc))
            if source != SOURCE_AUTO:
                raise
            continue

        _write_verified_cache(
            cache_path,
            metadata=metadata,
            config=config,
            source=current_source,
            content=content,
            force=force,
        )
        return _build_result(
            metadata=metadata,
            config=config,
            cache_path=cache_path,
            source=current_source,
            cached=False,
            verified_file_count=len(content.file_hashes),
        )

    _raise_source_errors(errors)


def package_cache_path(config: SidecarConfig, input_package_hash: str) -> Path:
    """Return the deterministic local cache path for a package hash."""

    package_hash = _require_sha256("input_package_hash", input_package_hash)
    return config.data_dir / PACKAGES_DIR_NAME / package_hash


def verify_cached_input_package(
    package_path: Path,
    metadata: RoundMetadata,
    config: SidecarConfig,
) -> _VerifiedPackageContent:
    """Verify an existing local input package cache directory."""

    if not package_path.is_dir():
        raise InputPackageVerificationError(
            f"cached package path is not a directory: {package_path}"
        )

    package_hash = _require_sha256("input_package_hash", metadata.input_package_hash)
    bundle = _read_cached_json(package_path, BUNDLE_FILE_PATH)
    _verify_json_hash(
        BUNDLE_FILE_PATH,
        bundle,
        package_hash,
        context="input package boundary",
    )
    file_hashes = _parse_bundle(bundle, metadata, config)
    files = {BUNDLE_FILE_PATH: bundle}

    for file_path, expected_hash in file_hashes.items():
        content = _read_cached_json(package_path, file_path)
        _verify_json_hash(file_path, content, expected_hash, context="cached file")
        files[file_path] = content

    return _VerifiedPackageContent(files=files, file_hashes=file_hashes)


def canonical_json_hash(data: Any) -> str:
    canonical = json.dumps(data, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _download_verified_content(
    metadata: RoundMetadata,
    config: SidecarConfig,
    client: ScoringClient,
    *,
    source: ResolvedPackageSource,
    expected_package_hash: str,
) -> _VerifiedPackageContent:
    bundle = _fetch_package_file(client, metadata, config, source, BUNDLE_FILE_PATH)
    _verify_json_hash(
        BUNDLE_FILE_PATH,
        bundle,
        expected_package_hash,
        context=f"{source} input package boundary",
    )
    file_hashes = _parse_bundle(bundle, metadata, config)
    files = {BUNDLE_FILE_PATH: bundle}

    for file_path, expected_hash in file_hashes.items():
        content = _fetch_package_file(client, metadata, config, source, file_path)
        _verify_json_hash(file_path, content, expected_hash, context=f"{source} file")
        files[file_path] = content

    return _VerifiedPackageContent(files=files, file_hashes=file_hashes)


def _fetch_package_file(
    client: ScoringClient,
    metadata: RoundMetadata,
    config: SidecarConfig,
    source: ResolvedPackageSource,
    file_path: str,
) -> dict[str, Any] | list[Any]:
    try:
        if source == SOURCE_HTTPS:
            return client.fetch_input_package_file(metadata.round_number, file_path)
        if source == SOURCE_IPFS:
            if config.ipfs_gateway_url is None:
                raise InputPackageDownloadError(
                    "No IPFS gateway URL is configured; pass --ipfs-gateway-url "
                    "or set POSTFIAT_SIDECAR_IPFS_GATEWAY_URL"
                )
            return client.fetch_ipfs_package_file(
                config.ipfs_gateway_url,
                metadata.input_package_cid,
                file_path,
            )
    except ScoringClientError as exc:
        raise InputPackageDownloadError(str(exc)) from exc

    raise InputPackageDownloadError(f"unsupported package source: {source}")


def _parse_bundle(
    bundle: dict[str, Any] | list[Any],
    metadata: RoundMetadata,
    config: SidecarConfig,
) -> dict[str, str]:
    if not isinstance(bundle, dict):
        raise InputPackageVerificationError("bundle.json must be a JSON object")
    if bundle.get("package_kind") != "input":
        raise InputPackageVerificationError("bundle.json package_kind must be input")
    if bundle.get("network") != config.network:
        raise InputPackageVerificationError(
            "bundle.json network "
            f"{bundle.get('network')!r} does not match configured network "
            f"{config.network!r}"
        )
    if bundle.get("round_number") != metadata.round_number:
        raise InputPackageVerificationError(
            "bundle.json round_number does not match round metadata"
        )
    if bundle.get("input_frozen_at") != metadata.input_frozen_at:
        raise InputPackageVerificationError(
            "bundle.json input_frozen_at does not match round metadata"
        )

    raw_file_hashes = bundle.get("file_hashes")
    if not isinstance(raw_file_hashes, dict) or not raw_file_hashes:
        raise InputPackageVerificationError(
            "bundle.json file_hashes must be a non-empty object"
        )

    file_hashes: dict[str, str] = {}
    for raw_path, raw_hash in raw_file_hashes.items():
        if not isinstance(raw_path, str):
            raise InputPackageVerificationError(
                "bundle.json file_hashes paths must be strings"
            )
        file_path = _validate_package_path(raw_path)
        if file_path == BUNDLE_FILE_PATH:
            raise InputPackageVerificationError(
                "bundle.json must not be listed in file_hashes"
            )
        if not isinstance(raw_hash, str):
            raise InputPackageVerificationError(
                f"bundle.json file hash for {file_path} must be a string"
            )
        file_hashes[file_path] = _require_sha256(
            f"bundle.json file hash for {file_path}",
            raw_hash,
        )

    return dict(sorted(file_hashes.items()))


def _validate_package_path(file_path: str) -> str:
    stripped = file_path.strip()
    if not stripped or stripped != file_path:
        raise InputPackageVerificationError(
            f"invalid package file path in bundle.json: {file_path!r}"
        )
    if "\\" in stripped:
        raise InputPackageVerificationError(
            f"package file path must use POSIX separators: {file_path!r}"
        )

    path = PurePosixPath(stripped)
    if (
        path.is_absolute()
        or path.as_posix() != stripped
        or any(part in {"", ".", ".."} for part in path.parts)
    ):
        raise InputPackageVerificationError(
            f"unsafe package file path in bundle.json: {file_path!r}"
        )
    return path.as_posix()


def _verify_json_hash(
    file_path: str,
    content: Any,
    expected_hash: str,
    *,
    context: str,
) -> None:
    actual_hash = canonical_json_hash(content)
    if actual_hash != expected_hash:
        raise InputPackageVerificationError(
            f"{context} hash mismatch for {file_path}: expected "
            f"{expected_hash}, got {actual_hash}"
        )


def _require_sha256(field: str, value: str) -> str:
    normalized = value.lower()
    if len(normalized) != 64 or any(
        char not in "0123456789abcdef" for char in normalized
    ):
        raise InputPackageVerificationError(f"{field} must be a 64-character hex hash")
    return normalized


def _source_order(source: PackageSource) -> tuple[ResolvedPackageSource, ...]:
    if source == SOURCE_AUTO:
        return (SOURCE_HTTPS, SOURCE_IPFS)
    return (source,)


def _raise_source_errors(errors: list[tuple[str, InputPackageError]]) -> NoReturn:
    details = "; ".join(f"{source}: {error}" for source, error in errors)
    message = f"Unable to fetch a verified input package ({details})"
    if any(isinstance(error, InputPackageVerificationError) for _, error in errors):
        raise InputPackageVerificationError(message)
    raise InputPackageDownloadError(message)


def _write_verified_cache(
    cache_path: Path,
    *,
    metadata: RoundMetadata,
    config: SidecarConfig,
    source: ResolvedPackageSource,
    content: _VerifiedPackageContent,
    force: bool,
) -> None:
    packages_dir = cache_path.parent
    packages_dir.mkdir(parents=True, exist_ok=True)
    temp_path = packages_dir / f".{cache_path.name}.tmp-{uuid.uuid4().hex}"
    backup_path: Path | None = None

    try:
        _write_package_files(temp_path, content.files)
        _write_local_metadata(
            temp_path,
            metadata=metadata,
            config=config,
            source=source,
            verified_file_count=len(content.file_hashes),
        )

        if cache_path.exists():
            if not force:
                raise InputPackageCacheError(
                    f"cache path already exists: {cache_path}"
                )
            backup_path = packages_dir / f".{cache_path.name}.old-{uuid.uuid4().hex}"
            cache_path.rename(backup_path)

        temp_path.rename(cache_path)
        if backup_path is not None:
            _remove_path(backup_path)
    except InputPackageError:
        _remove_path(temp_path)
        if backup_path is not None and backup_path.exists() and not cache_path.exists():
            backup_path.rename(cache_path)
        raise
    except OSError as exc:
        _remove_path(temp_path)
        if backup_path is not None and backup_path.exists() and not cache_path.exists():
            backup_path.rename(cache_path)
        raise InputPackageCacheError(
            f"Failed to write verified input package cache at {cache_path}: {exc}"
        ) from exc


def _write_package_files(root: Path, files: dict[str, Any]) -> None:
    for file_path, content in files.items():
        _write_json(root, file_path, content)


def _write_local_metadata(
    root: Path,
    *,
    metadata: RoundMetadata,
    config: SidecarConfig,
    source: ResolvedPackageSource,
    verified_file_count: int,
) -> None:
    local_metadata = {
        "sidecar_metadata_version": SIDECAR_METADATA_VERSION,
        "round_id": metadata.round_id,
        "round_number": metadata.round_number,
        "network": config.network,
        "input_package_cid": metadata.input_package_cid,
        "input_package_hash": _require_sha256(
            "input_package_hash",
            metadata.input_package_hash,
        ),
        "input_frozen_at": metadata.input_frozen_at,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "source": source,
        "verified_file_count": verified_file_count,
    }
    _write_json(root, SIDECAR_METADATA_FILE_PATH, local_metadata)


def _write_json(root: Path, file_path: str, content: Any) -> None:
    target = _resolve_cache_file(root, file_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(content, indent=2, sort_keys=True, default=str) + "\n",
        encoding="utf-8",
    )


def _read_cached_json(root: Path, file_path: str) -> Any:
    target = _resolve_cache_file(root, file_path)
    try:
        return json.loads(target.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise InputPackageVerificationError(
            f"cached package file is missing: {file_path}"
        ) from exc
    except json.JSONDecodeError as exc:
        raise InputPackageVerificationError(
            f"cached package file is not valid JSON: {file_path}"
        ) from exc


def _resolve_cache_file(root: Path, file_path: str) -> Path:
    safe_path = _validate_cache_path(file_path)
    return root.joinpath(*PurePosixPath(safe_path).parts)


def _validate_cache_path(file_path: str) -> str:
    if file_path == SIDECAR_METADATA_FILE_PATH:
        return file_path
    return _validate_package_path(file_path)


def _remove_path(path: Path) -> None:
    if not path.exists():
        return
    if path.is_dir():
        shutil.rmtree(path, ignore_errors=True)
    else:
        path.unlink(missing_ok=True)


def _build_result(
    *,
    metadata: RoundMetadata,
    config: SidecarConfig,
    cache_path: Path,
    source: ResolvedPackageSource,
    cached: bool,
    verified_file_count: int,
) -> FetchedInputPackage:
    return FetchedInputPackage(
        round_id=metadata.round_id,
        round_number=metadata.round_number,
        network=config.network,
        input_package_cid=metadata.input_package_cid,
        input_package_hash=_require_sha256(
            "input_package_hash",
            metadata.input_package_hash,
        ),
        input_frozen_at=metadata.input_frozen_at,
        source=source,
        cached=cached,
        local_path=cache_path,
        verified_file_count=verified_file_count,
    )
