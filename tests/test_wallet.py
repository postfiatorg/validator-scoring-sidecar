import pytest
from xrpl.wallet import Wallet

from validator_scoring_sidecar.wallet import relay_wallet_from_secret

# Canonical, public test vectors — neither is anyone's real wallet.
# The all-"abandon" phrase (with the "art" checksum word) is the standard BIP39
# 24-word test mnemonic; its XRPL account is fixed by the m/44'/144'/0'/0/0 path.
ABANDON_24 = (
    "abandon abandon abandon abandon abandon abandon abandon abandon "
    "abandon abandon abandon abandon abandon abandon abandon abandon "
    "abandon abandon abandon abandon abandon abandon abandon art"
)
ABANDON_24_ADDRESS = "rKxpJQ6hLWYbo7p1oo7WHjrcrRFv1TUQeC"

# A well-known public XRPL "s..." family seed (the "masterpassphrase" seed).
SAMPLE_S_SEED = "snoPBrXtMeMyMHUVTgbuqAfg1SUTb"


def test_derives_account_from_24_word_bip39_phrase():
    wallet = relay_wallet_from_secret(ABANDON_24)
    assert wallet.classic_address == ABANDON_24_ADDRESS


def test_phrase_whitespace_is_normalized():
    padded = "  " + "   ".join(ABANDON_24.split()) + "  \n"
    assert relay_wallet_from_secret(padded).classic_address == ABANDON_24_ADDRESS


def test_still_accepts_s_family_seed():
    # The s... path is an unchanged pass-through to xrpl-py's Wallet.from_seed,
    # so it must resolve to exactly the same account as before.
    expected = Wallet.from_seed(SAMPLE_S_SEED).classic_address
    assert relay_wallet_from_secret(SAMPLE_S_SEED).classic_address == expected
    assert expected.startswith("r")


def test_s_seed_whitespace_is_trimmed():
    padded = f"  {SAMPLE_S_SEED}\n"
    assert (
        relay_wallet_from_secret(padded).classic_address
        == relay_wallet_from_secret(SAMPLE_S_SEED).classic_address
    )


def test_derived_wallet_is_signable():
    wallet = relay_wallet_from_secret(ABANDON_24)
    assert wallet.private_key
    assert wallet.public_key


def test_rejects_arbitrary_string():
    with pytest.raises(ValueError):
        relay_wallet_from_secret("not a seed or a phrase")


def test_rejects_phrase_with_bad_checksum():
    # 24 real words but the wrong final checksum word: not a valid BIP39 mnemonic,
    # and not an s... seed either.
    bad = " ".join(["abandon"] * 24)
    with pytest.raises(ValueError):
        relay_wallet_from_secret(bad)
