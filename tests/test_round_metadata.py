import pytest

from validator_scoring_sidecar.round_metadata import (
    MissingFrozenInputMetadata,
    RoundMetadata,
    RoundMetadataError,
)


def _payload(**overrides):
    payload = {
        "id": 123,
        "round_number": 456,
        "status": "COMPLETE",
        "input_package_cid": "QmInput",
        "input_package_hash": "a" * 64,
        "input_frozen_at": "2026-05-25T00:00:00+00:00",
        "final_bundle_cid": "QmFinal",
    }
    payload.update(overrides)
    return payload


def test_round_metadata_parses_frozen_input_fields():
    metadata = RoundMetadata.from_api_payload(
        _payload(
            status=" COMPLETE ",
            input_package_cid=" QmInput ",
            final_bundle_cid=" QmFinal ",
        ),
        requested_round_id=123,
    )

    assert metadata.as_dict() == {
        "round_id": 123,
        "round_number": 456,
        "status": "COMPLETE",
        "input_package_cid": "QmInput",
        "input_package_hash": "a" * 64,
        "input_frozen_at": "2026-05-25T00:00:00+00:00",
        "final_bundle_cid": "QmFinal",
    }


@pytest.mark.parametrize(
    "field",
    ["input_package_cid", "input_package_hash", "input_frozen_at"],
)
def test_round_metadata_requires_frozen_input_fields(field):
    payload = _payload(**{field: None})

    with pytest.raises(MissingFrozenInputMetadata) as exc_info:
        RoundMetadata.from_api_payload(payload, requested_round_id=123)

    assert field in exc_info.value.missing_fields
    assert "legacy, dry-run, override, or pre-M2.1" in str(exc_info.value)


def test_round_metadata_reports_all_missing_frozen_input_fields():
    payload = _payload(input_package_cid=None, input_frozen_at=" ")

    with pytest.raises(MissingFrozenInputMetadata) as exc_info:
        RoundMetadata.from_api_payload(payload, requested_round_id=123)

    assert exc_info.value.missing_fields == [
        "input_package_cid",
        "input_frozen_at",
    ]


def test_round_metadata_allows_missing_final_bundle_cid():
    metadata = RoundMetadata.from_api_payload(
        _payload(final_bundle_cid=None),
        requested_round_id=123,
    )

    assert metadata.final_bundle_cid is None


def test_round_metadata_treats_blank_final_bundle_cid_as_missing():
    metadata = RoundMetadata.from_api_payload(
        _payload(final_bundle_cid=" "),
        requested_round_id=123,
    )

    assert metadata.final_bundle_cid is None


def test_round_metadata_rejects_malformed_core_fields():
    with pytest.raises(RoundMetadataError, match="round_number"):
        RoundMetadata.from_api_payload(
            _payload(round_number="456"),
            requested_round_id=123,
        )


@pytest.mark.parametrize("field", ["id", "round_number"])
def test_round_metadata_rejects_non_positive_identifiers(field):
    with pytest.raises(RoundMetadataError, match=field):
        RoundMetadata.from_api_payload(
            _payload(**{field: 0}),
            requested_round_id=123,
        )
