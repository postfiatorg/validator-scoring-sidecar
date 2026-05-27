import json
from pathlib import Path

import httpx
import pytest

from validator_scoring_sidecar.config import load_config
from validator_scoring_sidecar.input_package import (
    SIDECAR_METADATA_FILE_PATH,
    InputPackageDownloadError,
    InputPackageVerificationError,
    canonical_json_hash,
    fetch_input_package,
    package_cache_path,
)
from validator_scoring_sidecar.round_metadata import RoundMetadata
from validator_scoring_sidecar.scoring_client import ScoringClient


def _package_files(*, network="testnet", round_number=456):
    files = {
        "inputs/model_request.json": {"messages": [{"role": "user", "content": "score"}]},
        "inputs/validator_evidence.json": {"validators": []},
        "runtime/execution_manifest.json": {
            "round": {"kind": "normal", "inference_performed": True}
        },
    }
    bundle = {
        "bundle_version": 2,
        "package_kind": "input",
        "round_kind": "normal",
        "network": network,
        "round_number": round_number,
        "input_frozen_at": "2026-05-25T00:00:00+00:00",
        "entrypoints": {
            "model_request": "inputs/model_request.json",
            "execution_manifest": "runtime/execution_manifest.json",
        },
        "file_hashes": {
            path: canonical_json_hash(content)
            for path, content in sorted(files.items())
        },
    }
    return bundle, files, canonical_json_hash(bundle)


def _metadata(package_hash):
    return RoundMetadata(
        round_id=123,
        round_number=456,
        status="COMPLETE",
        input_package_cid="QmInput",
        input_package_hash=package_hash,
        input_frozen_at="2026-05-25T00:00:00+00:00",
        final_bundle_cid="QmFinal",
    )


def _config(tmp_path: Path, *, network="testnet"):
    return load_config(
        base_url="https://scoring.example.org",
        data_dir=tmp_path,
        ipfs_gateway_url="https://ipfs.example.org/ipfs",
        network=network,
        environ={},
    )


def _client(config, handler):
    return ScoringClient(
        config,
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )


def _https_path(file_path):
    return f"/api/scoring/rounds/456/input/{file_path}"


def _ipfs_path(file_path):
    return f"/ipfs/QmInput/{file_path}"


def test_fetch_input_package_from_https_writes_verified_cache(tmp_path):
    bundle, files, package_hash = _package_files()
    metadata = _metadata(package_hash)
    config = _config(tmp_path)

    def handler(request):
        if request.url.path == _https_path("bundle.json"):
            return httpx.Response(200, json=bundle)
        for file_path, content in files.items():
            if request.url.path == _https_path(file_path):
                return httpx.Response(200, json=content)
        return httpx.Response(404)

    result = fetch_input_package(
        metadata,
        config,
        _client(config, handler),
        source="https",
    )

    assert result.source == "https"
    assert result.cached is False
    assert result.local_path == package_cache_path(config, package_hash)
    assert result.verified_file_count == len(files)
    assert (result.local_path / "bundle.json").is_file()
    assert (result.local_path / "inputs/model_request.json").is_file()

    local_metadata = json.loads(
        (result.local_path / SIDECAR_METADATA_FILE_PATH).read_text(encoding="utf-8")
    )
    assert local_metadata["round_id"] == 123
    assert local_metadata["round_number"] == 456
    assert local_metadata["network"] == "testnet"
    assert local_metadata["input_package_cid"] == "QmInput"
    assert local_metadata["input_package_hash"] == package_hash
    assert local_metadata["source"] == "https"
    assert local_metadata["verified_file_count"] == len(files)


def test_fetch_input_package_forced_ipfs_uses_gateway(tmp_path):
    bundle, files, package_hash = _package_files()
    metadata = _metadata(package_hash)
    config = _config(tmp_path)
    requested_paths = []

    def handler(request):
        requested_paths.append(request.url.path)
        if request.url.path == _ipfs_path("bundle.json"):
            return httpx.Response(200, json=bundle)
        for file_path, content in files.items():
            if request.url.path == _ipfs_path(file_path):
                return httpx.Response(200, json=content)
        return httpx.Response(404)

    result = fetch_input_package(
        metadata,
        config,
        _client(config, handler),
        source="ipfs",
    )

    assert result.source == "ipfs"
    assert _ipfs_path("bundle.json") in requested_paths
    assert all(not path.startswith("/api/scoring/") for path in requested_paths)


def test_fetch_input_package_auto_falls_back_to_ipfs(tmp_path):
    bundle, files, package_hash = _package_files()
    metadata = _metadata(package_hash)
    config = _config(tmp_path)

    def handler(request):
        if request.url.host == "scoring.example.org":
            return httpx.Response(404)
        if request.url.path == _ipfs_path("bundle.json"):
            return httpx.Response(200, json=bundle)
        for file_path, content in files.items():
            if request.url.path == _ipfs_path(file_path):
                return httpx.Response(200, json=content)
        return httpx.Response(404)

    result = fetch_input_package(metadata, config, _client(config, handler))

    assert result.source == "ipfs"
    assert result.verified_file_count == len(files)


def test_fetch_input_package_reuses_verified_cache(tmp_path):
    bundle, files, package_hash = _package_files()
    metadata = _metadata(package_hash)
    config = _config(tmp_path)

    def first_handler(request):
        if request.url.path == _https_path("bundle.json"):
            return httpx.Response(200, json=bundle)
        for file_path, content in files.items():
            if request.url.path == _https_path(file_path):
                return httpx.Response(200, json=content)
        return httpx.Response(404)

    fetch_input_package(metadata, config, _client(config, first_handler), source="https")

    def failing_handler(request):
        raise AssertionError("cached package should not trigger HTTP requests")

    result = fetch_input_package(
        metadata,
        config,
        _client(config, failing_handler),
        source="https",
    )

    assert result.source == "cache"
    assert result.cached is True


def test_fetch_input_package_rejects_package_hash_mismatch(tmp_path):
    bundle, files, package_hash = _package_files()
    metadata = _metadata("b" * 64)
    config = _config(tmp_path)

    def handler(request):
        if request.url.path == _https_path("bundle.json"):
            return httpx.Response(200, json=bundle)
        for file_path, content in files.items():
            if request.url.path == _https_path(file_path):
                return httpx.Response(200, json=content)
        return httpx.Response(404)

    with pytest.raises(InputPackageVerificationError, match="hash mismatch"):
        fetch_input_package(metadata, config, _client(config, handler), source="https")

    assert not package_cache_path(config, package_hash).exists()
    assert not package_cache_path(config, "b" * 64).exists()


def test_fetch_input_package_rejects_cross_network_package(tmp_path):
    bundle, files, package_hash = _package_files(network="devnet")
    metadata = _metadata(package_hash)
    config = _config(tmp_path, network="testnet")

    def handler(request):
        if request.url.path == _https_path("bundle.json"):
            return httpx.Response(200, json=bundle)
        for file_path, content in files.items():
            if request.url.path == _https_path(file_path):
                return httpx.Response(200, json=content)
        return httpx.Response(404)

    with pytest.raises(InputPackageVerificationError, match="does not match"):
        fetch_input_package(metadata, config, _client(config, handler), source="https")

    assert not package_cache_path(config, package_hash).exists()


def test_fetch_input_package_failed_download_leaves_no_cache(tmp_path):
    bundle, _, package_hash = _package_files()
    metadata = _metadata(package_hash)
    config = _config(tmp_path)

    def handler(request):
        if request.url.path == _https_path("bundle.json"):
            return httpx.Response(200, json=bundle)
        return httpx.Response(404)

    with pytest.raises(InputPackageDownloadError, match="HTTP 404"):
        fetch_input_package(metadata, config, _client(config, handler), source="https")

    cache_path = package_cache_path(config, package_hash)
    assert not cache_path.exists()
    assert not any(cache_path.parent.glob(f".{package_hash}.tmp-*"))
