import json

import pytest

from validator_scoring_sidecar.chain import (
    AnnouncementError,
    VerifiedAnnouncement,
    WatchedTransaction,
    decode_and_verify_announcement,
    decode_round_announcement,
    verify_announced_package,
)
from validator_scoring_sidecar.config import load_config
from validator_scoring_sidecar.failure import FailureCategory
from validator_scoring_sidecar.input_package import FetchedInputPackage
from validator_scoring_sidecar.scoring import commit_reveal

PUBLISHER = "rFoundationPublisher"
NETWORK = "testnet"
CID = "QmYwAPJzv5CZsnA625s3Xf2nemtYgPpHdWEz79ojWnPbdG"
HASH = "a" * 64
OTHER_CID = "QmPZ9gcCEpqKTo6aq61g2nXGUhM4iCL3ewB6LDXZCtioEB"


def _announcement_payload(**overrides):
    payload = {
        "protocol_version": 1,
        "network": NETWORK,
        "round_number": 456,
        "input_package_cid": CID,
        "input_package_hash": HASH,
        "commit_opens_at": "2026-05-25T00:00:00+00:00",
        "commit_closes_at": "2026-05-25T00:30:00+00:00",
        "reveal_opens_at": "2026-05-25T00:30:00+00:00",
        "reveal_closes_at": "2026-05-25T01:00:00+00:00",
    }
    payload.update(overrides)
    return payload


def _hex(text):
    return text.encode("utf-8").hex()


def _memo(memo_type, memo_data_obj):
    return {
        "Memo": {
            "MemoType": _hex(memo_type),
            "MemoData": _hex(json.dumps(memo_data_obj)),
        }
    }


def _announcement_memo(payload=None):
    return _memo(
        commit_reveal.ROUND_ANNOUNCEMENT_TYPE,
        payload if payload is not None else _announcement_payload(),
    )


def _tx(memos):
    return WatchedTransaction(
        tx_hash="ABC123",
        ledger_index=100,
        account=PUBLISHER,
        memos=memos,
        tx={},
    )


def _round_payload(**overrides):
    payload = {
        "id": 123,
        "round_number": 456,
        "status": "INPUT_FROZEN",
        "input_package_cid": CID,
        "input_package_hash": HASH,
        "input_frozen_at": "2026-05-25T00:00:00+00:00",
        "final_bundle_cid": None,
    }
    payload.update(overrides)
    return payload


class FakeClient:
    def __init__(self, rounds):
        self.rounds = rounds

    def fetch_rounds(self, *, limit, offset=0):
        return list(self.rounds)


class FakeFetcher:
    def __init__(self):
        self.calls = []

    def __call__(self, metadata, config, client, *, source, force):
        self.calls.append(metadata)
        return FetchedInputPackage(
            round_id=metadata.round_id,
            round_number=metadata.round_number,
            network=config.network,
            input_package_cid=metadata.input_package_cid,
            input_package_hash=metadata.input_package_hash,
            input_frozen_at=metadata.input_frozen_at,
            source="https",
            cached=False,
            local_path=config.data_dir / "packages" / metadata.input_package_hash,
            verified_file_count=3,
        )


def _config(tmp_path):
    return load_config(network=NETWORK, data_dir=tmp_path, environ={})


def test_decode_returns_validated_announcement():
    announcement = decode_round_announcement(_tx([_announcement_memo()]))
    assert isinstance(announcement, commit_reveal.RoundAnnouncement)
    assert announcement.round_number == 456
    assert announcement.input_package_cid == CID
    assert announcement.input_package_hash == HASH


def test_decode_returns_none_without_announcement_memo():
    assert decode_round_announcement(_tx([_memo("pf_other_memo_v1", {"x": 1})])) is None
    assert decode_round_announcement(_tx([])) is None


def test_decode_picks_announcement_among_other_memos():
    memos = [_memo("pf_other_memo_v1", {"x": 1}), _announcement_memo()]
    announcement = decode_round_announcement(_tx(memos))
    assert announcement is not None
    assert announcement.round_number == 456


def test_decode_skips_malformed_memo_entries():
    memos = [
        {"NotMemo": {}},
        "garbage",
        {"Memo": {"MemoType": "zz"}},
        _announcement_memo(),
    ]
    announcement = decode_round_announcement(_tx(memos))
    assert announcement is not None
    assert announcement.round_number == 456


def test_decode_raises_on_non_hex_memo_data():
    memo = {
        "Memo": {
            "MemoType": _hex(commit_reveal.ROUND_ANNOUNCEMENT_TYPE),
            "MemoData": "zzzz",
        }
    }
    with pytest.raises(AnnouncementError) as exc:
        decode_round_announcement(_tx([memo]))
    assert exc.value.category == FailureCategory.MANIFEST_UNSUPPORTED


def test_decode_raises_on_non_json_memo_data():
    memo = {
        "Memo": {
            "MemoType": _hex(commit_reveal.ROUND_ANNOUNCEMENT_TYPE),
            "MemoData": _hex("not json"),
        }
    }
    with pytest.raises(AnnouncementError):
        decode_round_announcement(_tx([memo]))


def test_decode_raises_on_extra_field():
    payload = _announcement_payload(round_kind="normal")
    with pytest.raises(AnnouncementError):
        decode_round_announcement(_tx([_announcement_memo(payload)]))


def test_decode_raises_on_missing_field():
    payload = _announcement_payload()
    del payload["reveal_closes_at"]
    with pytest.raises(AnnouncementError):
        decode_round_announcement(_tx([_announcement_memo(payload)]))


def test_decode_returns_first_announcement_memo():
    memos = [
        _announcement_memo(_announcement_payload(round_number=111)),
        _announcement_memo(_announcement_payload(round_number=222)),
    ]
    announcement = decode_round_announcement(_tx(memos))
    assert announcement.round_number == 111


def test_decode_raises_when_matched_memo_has_no_memo_data():
    memo = {"Memo": {"MemoType": _hex(commit_reveal.ROUND_ANNOUNCEMENT_TYPE)}}
    with pytest.raises(AnnouncementError):
        decode_round_announcement(_tx([memo]))


def test_verify_binds_matching_round(tmp_path):
    config = _config(tmp_path)
    client = FakeClient([_round_payload()])
    fetcher = FakeFetcher()
    announcement = decode_round_announcement(_tx([_announcement_memo()]))

    package = verify_announced_package(
        announcement, config, client, package_fetcher=fetcher
    )

    assert package.input_package_hash == HASH
    assert package.input_package_cid == CID
    assert len(fetcher.calls) == 1
    assert fetcher.calls[0].round_number == 456


def test_verify_raises_on_network_mismatch(tmp_path):
    config = _config(tmp_path)  # testnet
    client = FakeClient([_round_payload()])
    announcement = decode_round_announcement(
        _tx([_announcement_memo(_announcement_payload(network="devnet"))])
    )

    with pytest.raises(AnnouncementError) as exc:
        verify_announced_package(
            announcement, config, client, package_fetcher=FakeFetcher()
        )
    assert exc.value.category == FailureCategory.MANIFEST_UNSUPPORTED


def test_verify_raises_when_no_round_matches_hash(tmp_path):
    config = _config(tmp_path)
    client = FakeClient([_round_payload(input_package_hash="b" * 64)])
    announcement = decode_round_announcement(_tx([_announcement_memo()]))

    with pytest.raises(AnnouncementError):
        verify_announced_package(
            announcement, config, client, package_fetcher=FakeFetcher()
        )


def test_verify_raises_on_cid_mismatch(tmp_path):
    config = _config(tmp_path)
    client = FakeClient([_round_payload(input_package_cid=OTHER_CID)])
    announcement = decode_round_announcement(_tx([_announcement_memo()]))

    with pytest.raises(AnnouncementError):
        verify_announced_package(
            announcement, config, client, package_fetcher=FakeFetcher()
        )


def test_decode_and_verify_end_to_end(tmp_path):
    config = _config(tmp_path)
    client = FakeClient([_round_payload()])

    result = decode_and_verify_announcement(
        _tx([_announcement_memo()]), config, client, package_fetcher=FakeFetcher()
    )

    assert isinstance(result, VerifiedAnnouncement)
    assert result.announcement.round_number == 456
    assert result.package.input_package_hash == HASH


def test_decode_and_verify_returns_none_for_non_announcement(tmp_path):
    config = _config(tmp_path)
    client = FakeClient([_round_payload()])

    result = decode_and_verify_announcement(
        _tx([_memo("pf_other_v1", {"x": 1})]),
        config,
        client,
        package_fetcher=FakeFetcher(),
    )

    assert result is None
