"""Tests for the maintainer vendor-freshness command."""

import argparse

import pytest

from scripts import check_vendor_freshness


class _Response:
    def __init__(self, content: bytes):
        self.content = content

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self) -> bytes:
        return self.content


def test_fetch_supports_contributor_repository_and_branch(monkeypatch):
    observed = {}

    def fake_urlopen(url, *, timeout):
        observed.update(url=url, timeout=timeout)
        return _Response(b"selector source")

    monkeypatch.setattr(check_vendor_freshness.urllib.request, "urlopen", fake_urlopen)

    content = check_vendor_freshness._fetch(
        "citadelculture/dynamic-unl-scoring",
        "codex/unl-tie-break-weakest",
        check_vendor_freshness.SELECTOR_PATH,
    )

    assert content == b"selector source"
    assert observed == {
        "url": (
            "https://raw.githubusercontent.com/"
            "citadelculture/dynamic-unl-scoring/"
            "codex/unl-tie-break-weakest/"
            "scoring_service/services/unl_selector.py"
        ),
        "timeout": check_vendor_freshness.HTTP_TIMEOUT_SECONDS,
    }


@pytest.mark.parametrize(
    "value",
    ["dynamic-unl-scoring", "owner/name/extra", "owner name/repository"],
)
def test_repository_requires_owner_name(value):
    with pytest.raises(argparse.ArgumentTypeError):
        check_vendor_freshness._repository(value)
