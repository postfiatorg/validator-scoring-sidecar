"""Tests for the manifest compatibility checker."""

from copy import deepcopy
from typing import Any

import pytest

from validator_scoring_sidecar.failure import FailureCategory
from validator_scoring_sidecar.manifest import (
    SUPPORTED_MANIFEST_SCHEMA_VERSIONS,
    CompatibilityResult,
    check_compatibility,
)
from validator_scoring_sidecar.scoring import (
    SUPPORTED_PARSER_CONTENT_HASHES,
    SUPPORTED_SELECTOR_CONTENT_HASHES,
)

PARSER_HASH = sorted(SUPPORTED_PARSER_CONTENT_HASHES)[0]
SELECTOR_HASH = sorted(SUPPORTED_SELECTOR_CONTENT_HASHES)[0]
MODEL_REVISION = "a" * 40
DEFAULT_ROUND_NUMBER = 100
IMAGE_REF = (
    "lmsysorg/sglang:nightly-dev-cu13-20260430-e60c60ef"
    "@sha256:5d9ec71597ade6b8237d61ae6f01b976cb3d5ad2c1e3cf4e0acaf27a9ff49a65"
)
WORKSPACE_BYTES = "2147483648"
LAUNCH_ARGS = [
    "--model-path",
    "Qwen/Qwen3.6-27B-FP8",
    "--served-model-name",
    "Qwen/Qwen3.6-27B-FP8",
    "--tp",
    "1",
    "--mem-fraction-static",
    "0.75",
    "--enable-deterministic-inference",
    "--trust-remote-code",
]


def _manifest() -> dict[str, Any]:
    return {
        "schema_version": 1,
        "round": {
            "kind": "normal",
            "network": "testnet",
            "round_number": DEFAULT_ROUND_NUMBER,
            "inference_performed": True,
            "published_at": "2026-05-18T00:00:00+00:00",
        },
        "model": {
            "provider": "huggingface",
            "repo_id": "Qwen/Qwen3.6-27B-FP8",
            "revision": MODEL_REVISION,
            "served_name": "Qwen/Qwen3.6-27B-FP8",
        },
        "runtime": {
            "kind": "modal_sglang",
            "image": IMAGE_REF,
            "gpu": "H100",
            "tensor_parallelism": 1,
            "launch_args": list(LAUNCH_ARGS),
            "environment": {
                "SGLANG_FLASHINFER_WORKSPACE_SIZE": WORKSPACE_BYTES,
            },
        },
        "request": {
            "type": "openai_chat_completions",
            "file": "inputs/model_request.json",
            "method": "chat.completions.create",
            "model": "Qwen/Qwen3.6-27B-FP8",
            "temperature": 0,
            "max_tokens": 16384,
            "response_format": {"type": "json_object"},
            "extra_body": {},
            "timeout_seconds": 2100,
        },
        "code": {
            "repository": "postfiatorg/dynamic-unl-scoring",
            "commit": "abc123def456abc123def456abc123def456abcd",
            "parser": {
                "module": "scoring_service.services.response_parser",
                "version": "git:abc123def456",
                "content_sha256": PARSER_HASH,
            },
            "selector": {
                "module": "scoring_service.services.unl_selector",
                "version": "git:abc123def456",
                "content_sha256": SELECTOR_HASH,
                "parameters": {
                    "score_cutoff": 40,
                    "max_size": 35,
                    "min_score_gap": 5,
                },
            },
        },
        "canonicalization": {
            "hash_algorithm": "sha256",
            "text_encoding": "utf-8",
            "json_encoding": {
                "sort_keys": True,
                "separators": [",", ":"],
                "default": "str",
            },
        },
    }


def _deployment(*, mode: str = "modal") -> dict[str, Any]:
    return {
        "mode": mode,
        "image": IMAGE_REF,
        "image_digest": (
            "5d9ec71597ade6b8237d61ae6f01b976cb3d5ad2c1e3cf4e0acaf27a9ff49a65"
        ),
        "launch_args": list(LAUNCH_ARGS),
        "gpu_class": "H100",
        "tensor_parallelism": 1,
        "environment": {
            "SGLANG_FLASHINFER_WORKSPACE_SIZE": WORKSPACE_BYTES,
        },
        "served_model_name": "Qwen/Qwen3.6-27B-FP8",
        "model_revision": MODEL_REVISION,
        "endpoint_url": "https://example.modal.run/v1",
        "gpu_mismatch_acknowledged": False,
        "deployed_at": "2026-05-18T00:00:00+00:00",
    }


def _check(
    manifest: dict[str, Any] | None = None,
    deployment: dict[str, Any] | None = None,
    *,
    sidecar_network: str = "testnet",
    expected_round_number: int = DEFAULT_ROUND_NUMBER,
) -> CompatibilityResult:
    return check_compatibility(
        manifest if manifest is not None else _manifest(),
        deployment if deployment is not None else _deployment(),
        sidecar_network=sidecar_network,
        expected_round_number=expected_round_number,
    )


# ---------------------------------------------------------------------------
# Happy paths
# ---------------------------------------------------------------------------


def test_compatible_modal_round_passes():
    result = _check()

    assert result.passed is True
    assert result.failure is None
    assert result.effective_mode == "modal"


def test_compatible_local_round_passes_when_runtime_kind_aligned():
    manifest = _manifest()
    manifest["runtime"]["kind"] = "local_sglang"

    result = _check(manifest, _deployment(mode="local"))

    assert result.passed is True
    assert result.effective_mode == "local"


def test_launch_args_pair_reordering_is_compatible():
    # Realistic operator reordering: swap whole flag/value pairs and move
    # boolean flags to different positions. The invariant a positional argv
    # parser needs ("value immediately follows its flag") is preserved.
    deployment = _deployment()
    deployment["launch_args"] = [
        "--enable-deterministic-inference",
        "--mem-fraction-static",
        "0.75",
        "--model-path",
        "Qwen/Qwen3.6-27B-FP8",
        "--trust-remote-code",
        "--tp",
        "1",
        "--served-model-name",
        "Qwen/Qwen3.6-27B-FP8",
    ]

    assert _check(deployment=deployment).passed is True


def test_launch_args_equals_form_equivalent_to_space_form():
    deployment = _deployment()
    deployment["launch_args"] = [
        "--model-path=Qwen/Qwen3.6-27B-FP8",
        "--served-model-name=Qwen/Qwen3.6-27B-FP8",
        "--tp=1",
        "--mem-fraction-static=0.75",
        "--enable-deterministic-inference",
        "--trust-remote-code",
    ]

    assert _check(deployment=deployment).passed is True


# ---------------------------------------------------------------------------
# Schema, round.kind, and short-circuit outcomes
# ---------------------------------------------------------------------------


def test_unsupported_schema_version_returns_manifest_unsupported():
    manifest = _manifest()
    manifest["schema_version"] = 999

    result = _check(manifest)

    assert result.failure.category == FailureCategory.MANIFEST_UNSUPPORTED
    assert result.failure.field == "schema_version"


def test_missing_schema_version_returns_manifest_unsupported():
    manifest = _manifest()
    del manifest["schema_version"]

    result = _check(manifest)

    assert result.failure.category == FailureCategory.MANIFEST_UNSUPPORTED
    assert result.failure.field == "schema_version"


def test_override_round_returns_skipped_override():
    manifest = _manifest()
    manifest["round"]["kind"] = "override"

    result = _check(manifest)

    assert result.failure.category == FailureCategory.SKIPPED_OVERRIDE


def test_dry_run_round_returns_skipped_operator_opt_out():
    manifest = _manifest()
    manifest["round"]["kind"] = "dry_run"

    result = _check(manifest)

    assert result.failure.category == FailureCategory.SKIPPED_OPERATOR_OPT_OUT


def test_unknown_round_kind_returns_manifest_incompatible():
    manifest = _manifest()
    manifest["round"]["kind"] = "totally_new_thing"

    result = _check(manifest)

    assert result.failure.category == FailureCategory.MANIFEST_INCOMPATIBLE
    assert result.failure.field == "round.kind"


# ---------------------------------------------------------------------------
# Round, network, deployment mode
# ---------------------------------------------------------------------------


def test_network_mismatch_against_sidecar_config_fails():
    result = _check(sidecar_network="devnet")

    assert result.failure.field == "round.network"


def test_inference_performed_must_be_true_for_normal_round():
    manifest = _manifest()
    manifest["round"]["inference_performed"] = False

    assert _check(manifest).failure.field == "round.inference_performed"


def test_round_number_must_be_integer():
    manifest = _manifest()
    manifest["round"]["round_number"] = None

    assert _check(manifest).failure.field == "round.round_number"


def test_round_number_mismatch_against_expected_fails():
    result = _check(expected_round_number=999)

    assert result.failure.field == "round.round_number"


def test_invalid_deployment_mode_fails():
    deployment = _deployment()
    deployment["mode"] = "shared_foundation"

    assert _check(deployment=deployment).failure.field == "deployment_record.mode"


# ---------------------------------------------------------------------------
# Model checks
# ---------------------------------------------------------------------------


def test_non_huggingface_provider_fails():
    manifest = _manifest()
    manifest["model"]["provider"] = "anthropic"

    assert _check(manifest).failure.field == "model.provider"


def test_repo_id_must_match_deployed_served_model_name():
    manifest = _manifest()
    manifest["model"]["repo_id"] = "Qwen/Qwen3.6-72B-FP8"

    assert _check(manifest).failure.field == "model.repo_id"


def test_served_name_must_match_deployed_served_model_name():
    manifest = _manifest()
    manifest["model"]["served_name"] = "Qwen/SomethingElse"

    assert _check(manifest).failure.field == "model.served_name"


@pytest.mark.parametrize(
    "bad_revision",
    ["main", "v1.0", "a" * 39, "a" * 41, "g" * 40, None],
)
def test_revision_must_be_full_40_hex_commit_hash(bad_revision):
    manifest = _manifest()
    manifest["model"]["revision"] = bad_revision

    assert _check(manifest).failure.field == "model.revision"


def test_revision_must_match_deployment():
    manifest = _manifest()
    manifest["model"]["revision"] = "b" * 40

    assert _check(manifest).failure.field == "model.revision"


# ---------------------------------------------------------------------------
# Runtime checks
# ---------------------------------------------------------------------------


def test_runtime_kind_must_match_backend_mode():
    # Manifest says modal_sglang but operator is in local mode.
    result = _check(deployment=_deployment(mode="local"))

    assert result.failure.field == "runtime.kind"


def test_image_must_include_sha256_digest():
    manifest = _manifest()
    manifest["runtime"]["image"] = "lmsysorg/sglang:nightly-dev-cu13"
    deployment = _deployment()
    deployment["image"] = "lmsysorg/sglang:nightly-dev-cu13"

    assert _check(manifest, deployment).failure.field == "runtime.image"


def test_image_must_match_deployment():
    manifest = _manifest()
    manifest["runtime"]["image"] = (
        "lmsysorg/sglang:different-tag@sha256:" + "f" * 64
    )

    assert _check(manifest).failure.field == "runtime.image"


def test_tensor_parallelism_must_match():
    manifest = _manifest()
    manifest["runtime"]["tensor_parallelism"] = 2

    assert _check(manifest).failure.field == "runtime.tensor_parallelism"


def test_gpu_mismatch_in_modal_mode_fails_without_override():
    deployment = _deployment(mode="modal")
    deployment["gpu_class"] = "A100"

    assert _check(deployment=deployment).failure.field == "runtime.gpu"


def test_gpu_mismatch_in_modal_mode_cannot_be_acknowledged():
    deployment = _deployment(mode="modal")
    deployment["gpu_class"] = "A100"
    deployment["gpu_mismatch_acknowledged"] = True

    assert _check(deployment=deployment).failure.field == "runtime.gpu"


def test_gpu_mismatch_in_local_mode_without_ack_fails():
    manifest = _manifest()
    manifest["runtime"]["kind"] = "local_sglang"
    deployment = _deployment(mode="local")
    deployment["gpu_class"] = "A100"
    deployment["gpu_mismatch_acknowledged"] = False

    assert _check(manifest, deployment).failure.field == "runtime.gpu"


def test_gpu_mismatch_in_local_mode_with_ack_passes_as_local_unverified():
    manifest = _manifest()
    manifest["runtime"]["kind"] = "local_sglang"
    deployment = _deployment(mode="local")
    deployment["gpu_class"] = "A100"
    deployment["gpu_mismatch_acknowledged"] = True

    result = _check(manifest, deployment)

    assert result.passed is True
    assert result.effective_mode == "local_unverified"


def test_gpu_mismatch_with_non_boolean_ack_value_still_fails():
    # `gpu_mismatch_acknowledged` is strictly compared to True; non-boolean
    # truthy values such as the string "true" do not count as acknowledged.
    manifest = _manifest()
    manifest["runtime"]["kind"] = "local_sglang"
    deployment = _deployment(mode="local")
    deployment["gpu_class"] = "A100"
    deployment["gpu_mismatch_acknowledged"] = "true"

    assert _check(manifest, deployment).failure.field == "runtime.gpu"


def test_launch_args_missing_deterministic_flag_in_manifest_fails():
    manifest = _manifest()
    manifest["runtime"]["launch_args"] = [
        arg for arg in LAUNCH_ARGS if arg != "--enable-deterministic-inference"
    ]
    deployment = _deployment()
    deployment["launch_args"] = list(manifest["runtime"]["launch_args"])

    assert _check(manifest, deployment).failure.field == "runtime.launch_args"


def test_launch_args_missing_deterministic_flag_in_deployment_fails():
    deployment = _deployment()
    deployment["launch_args"] = [
        arg for arg in LAUNCH_ARGS if arg != "--enable-deterministic-inference"
    ]

    assert _check(deployment=deployment).failure.field == "runtime.launch_args"


def test_launch_args_value_differs_fails():
    manifest = _manifest()
    args = list(LAUNCH_ARGS)
    args[args.index("--tp") + 1] = "2"
    manifest["runtime"]["launch_args"] = args

    assert _check(manifest).failure.field == "runtime.launch_args"


def test_workspace_environment_mismatch_fails():
    manifest = _manifest()
    manifest["runtime"]["environment"]["SGLANG_FLASHINFER_WORKSPACE_SIZE"] = "1024"

    assert (
        _check(manifest).failure.field
        == "runtime.environment.SGLANG_FLASHINFER_WORKSPACE_SIZE"
    )


def test_unrelated_environment_keys_are_ignored():
    manifest = _manifest()
    manifest["runtime"]["environment"]["HF_HOME"] = "/some/path"
    deployment = _deployment()
    deployment["environment"]["HF_HUB_CACHE"] = "/different/path"

    assert _check(manifest, deployment).passed is True


# ---------------------------------------------------------------------------
# Request checks
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "field,bad_value",
    [
        ("type", "openai_completions"),
        ("method", "completions.create"),
        ("model", "Qwen/SomethingElse"),
    ],
)
def test_request_required_string_fields_must_match_expected(field, bad_value):
    manifest = _manifest()
    manifest["request"][field] = bad_value

    assert _check(manifest).failure.field == f"request.{field}"


def test_request_temperature_must_be_zero():
    manifest = _manifest()
    manifest["request"]["temperature"] = 0.5

    assert _check(manifest).failure.field == "request.temperature"


def test_response_format_must_be_json_object():
    manifest = _manifest()
    manifest["request"]["response_format"] = {"type": "text"}

    assert _check(manifest).failure.field == "request.response_format"


@pytest.mark.parametrize("bad_max_tokens", [0, -1, "16384", None, 16.5])
def test_max_tokens_must_be_positive_integer(bad_max_tokens):
    manifest = _manifest()
    manifest["request"]["max_tokens"] = bad_max_tokens

    assert _check(manifest).failure.field == "request.max_tokens"


@pytest.mark.parametrize("bad_extra_body", ["{}", None, [], 42])
def test_extra_body_must_be_json_object(bad_extra_body):
    manifest = _manifest()
    manifest["request"]["extra_body"] = bad_extra_body

    assert _check(manifest).failure.field == "request.extra_body"


def test_extra_body_with_thinking_config_passes():
    manifest = _manifest()
    manifest["request"]["extra_body"] = {
        "chat_template_kwargs": {"enable_thinking": False}
    }

    assert _check(manifest).passed is True


# ---------------------------------------------------------------------------
# Code checks
# ---------------------------------------------------------------------------


def test_code_repository_must_be_present():
    manifest = _manifest()
    del manifest["code"]["repository"]

    assert _check(manifest).failure.field == "code.repository"


def test_code_commit_must_be_present():
    manifest = _manifest()
    del manifest["code"]["commit"]

    assert _check(manifest).failure.field == "code.commit"


def test_parser_content_hash_not_in_supported_set_fails():
    manifest = _manifest()
    manifest["code"]["parser"]["content_sha256"] = "0" * 64

    assert _check(manifest).failure.field == "code.parser.content_sha256"


def test_selector_content_hash_not_in_supported_set_fails():
    manifest = _manifest()
    manifest["code"]["selector"]["content_sha256"] = "0" * 64

    assert _check(manifest).failure.field == "code.selector.content_sha256"


# ---------------------------------------------------------------------------
# Canonicalization checks
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "field,bad_value",
    [
        ("hash_algorithm", "blake2b"),
        ("text_encoding", "latin-1"),
    ],
)
def test_canonicalization_top_level_must_match_expected(field, bad_value):
    manifest = _manifest()
    manifest["canonicalization"][field] = bad_value

    assert _check(manifest).failure.field == f"canonicalization.{field}"


def test_canonicalization_sort_keys_must_be_true():
    manifest = _manifest()
    manifest["canonicalization"]["json_encoding"]["sort_keys"] = False

    assert (
        _check(manifest).failure.field
        == "canonicalization.json_encoding.sort_keys"
    )


def test_canonicalization_separators_must_be_comma_colon():
    manifest = _manifest()
    manifest["canonicalization"]["json_encoding"]["separators"] = [", ", ": "]

    assert (
        _check(manifest).failure.field
        == "canonicalization.json_encoding.separators"
    )


# ---------------------------------------------------------------------------
# Misc
# ---------------------------------------------------------------------------


def test_fixture_builders_produce_compatible_records():
    # Sanity check: the fixtures themselves do not drift apart silently.
    manifest = _manifest()
    deployment = _deployment()
    assert deepcopy(manifest) == _manifest()
    assert deepcopy(deployment) == _deployment()
    assert SUPPORTED_MANIFEST_SCHEMA_VERSIONS
