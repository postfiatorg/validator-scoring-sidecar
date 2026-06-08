"""Round metadata parsing for frozen input package inspection."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

FROZEN_INPUT_FIELDS = (
    "input_package_cid",
    "input_package_hash",
    "input_frozen_at",
)


class RoundMetadataError(ValueError):
    """Base error for malformed round metadata."""


class MissingFrozenInputMetadata(RoundMetadataError):
    """Raised when a round is not suitable for frozen input inspection."""

    def __init__(self, round_identifier: int, missing_fields: list[str]):
        self.round_identifier = round_identifier
        self.missing_fields = missing_fields
        fields = ", ".join(missing_fields)
        super().__init__(
            "Round "
            f"{round_identifier} does not expose frozen input package metadata "
            f"({fields}). It may be a legacy, dry-run, override, or round "
            "created before frozen input metadata was introduced."
        )


@dataclass(frozen=True)
class RoundMetadata:
    """Frozen input metadata exposed by the scoring service round endpoint."""

    round_id: int
    round_number: int
    status: str
    input_package_cid: str
    input_package_hash: str
    input_frozen_at: str
    final_bundle_cid: str | None

    @classmethod
    def from_api_payload(
        cls,
        payload: dict[str, Any],
        *,
        requested_round_id: int,
    ) -> "RoundMetadata":
        """Parse and validate scoring service round metadata."""

        round_id = _require_positive_int(payload, "id")
        round_number = _require_positive_int(payload, "round_number")
        status = _require_string(payload, "status")
        frozen_input = _required_frozen_input_fields(
            payload,
            requested_round_id,
        )

        final_bundle_cid = _optional_string(payload, "final_bundle_cid")

        return cls(
            round_id=round_id,
            round_number=round_number,
            status=status,
            input_package_cid=frozen_input["input_package_cid"],
            input_package_hash=frozen_input["input_package_hash"],
            input_frozen_at=frozen_input["input_frozen_at"],
            final_bundle_cid=final_bundle_cid,
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "round_id": self.round_id,
            "round_number": self.round_number,
            "status": self.status,
            "input_package_cid": self.input_package_cid,
            "input_package_hash": self.input_package_hash,
            "input_frozen_at": self.input_frozen_at,
            "final_bundle_cid": self.final_bundle_cid,
        }


def round_identifier(payload: dict[str, Any]) -> int:
    """Return the payload's positive integer round id, or 0 if absent/invalid.

    Round-list entries are not guaranteed to carry a usable ``id``; the value is
    used only to label round-metadata errors.
    """
    value = payload.get("id")
    return value if isinstance(value, int) and value > 0 else 0


def _required_frozen_input_fields(
    payload: dict[str, Any],
    requested_round_id: int,
) -> dict[str, str]:
    frozen_input: dict[str, str] = {}
    missing: list[str] = []
    for field in FROZEN_INPUT_FIELDS:
        value = payload.get(field)
        if not isinstance(value, str) or not value.strip():
            missing.append(field)
        else:
            frozen_input[field] = value.strip()

    if missing:
        raise MissingFrozenInputMetadata(requested_round_id, missing)

    return frozen_input


def _require_string(payload: dict[str, Any], field: str) -> str:
    value = payload.get(field)
    if not isinstance(value, str) or not value.strip():
        raise RoundMetadataError(f"{field} must be a non-empty string")
    return value.strip()


def _optional_string(payload: dict[str, Any], field: str) -> str | None:
    value = payload.get(field)
    if value is None:
        return None
    if not isinstance(value, str):
        raise RoundMetadataError(f"{field} must be a string or null")
    stripped = value.strip()
    return stripped or None


def _require_positive_int(payload: dict[str, Any], field: str) -> int:
    value = payload.get(field)
    if isinstance(value, bool) or not isinstance(value, int):
        raise RoundMetadataError(f"{field} must be an integer")
    if value <= 0:
        raise RoundMetadataError(f"{field} must be greater than zero")
    return value
