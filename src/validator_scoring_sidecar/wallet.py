"""Relay wallet construction for the participation sidecar.

The operator's relay wallet — the funded account that pays for and sends the
commit and reveal transactions — is configured as a single secret. Two formats
are accepted:

- an XRPL ``s...`` family seed, and
- a 24-word BIP39 recovery phrase, which is what Post Fiat's Task Node hands an
  operator when it creates their wallet.

A BIP39 phrase is derived down the standard XRPL account path so the resulting
``r...`` address is exactly the account Task Node created and funded; an ``s...``
seed is loaded as before. This lets an operator paste whichever value Task Node
gave them without converting it by hand.
"""

from __future__ import annotations

from xrpl.wallet import Wallet

# Standard XRPL BIP44 account path (m/44'/144'/0'/0/0, secp256k1). This is how
# Task Node — and XRPL wallets generally — derive an account from a BIP39
# recovery phrase, so deriving down the same path yields the funded account
# rather than a different, unfunded one.
_XRPL_BIP44_ACCOUNT = 0
_XRPL_BIP44_ADDRESS_INDEX = 0
# xrpl-py encodes a secp256k1 private key as the 32-byte key with a "00" prefix.
_SECP256K1_PRIVATE_KEY_PREFIX = "00"


def relay_wallet_from_secret(secret: str) -> Wallet:
    """Build the relay wallet from a BIP39 recovery phrase or an XRPL ``s...`` seed.

    Detects the format and loads accordingly: a valid BIP39 mnemonic (Task Node
    issues a 24-word one) is derived down the standard XRPL path; anything else
    is tried as an ``s...`` family seed. Raises ``ValueError`` when the secret is
    neither.
    """
    normalized = " ".join(secret.split())
    if _is_bip39_mnemonic(normalized):
        return _wallet_from_mnemonic(normalized)
    try:
        return Wallet.from_seed(secret.strip())
    except Exception as exc:
        raise ValueError(
            "relay wallet secret is neither a valid BIP39 recovery phrase "
            "nor an XRPL s... seed"
        ) from exc


def _is_bip39_mnemonic(candidate: str) -> bool:
    from bip_utils import Bip39Languages, Bip39MnemonicValidator

    return Bip39MnemonicValidator(Bip39Languages.ENGLISH).IsValid(candidate)


def _wallet_from_mnemonic(mnemonic: str) -> Wallet:
    from bip_utils import (
        Bip39Languages,
        Bip39SeedGenerator,
        Bip44,
        Bip44Changes,
        Bip44Coins,
    )

    seed_bytes = Bip39SeedGenerator(mnemonic, Bip39Languages.ENGLISH).Generate()
    account = (
        Bip44.FromSeed(seed_bytes, Bip44Coins.RIPPLE)
        .Purpose()
        .Coin()
        .Account(_XRPL_BIP44_ACCOUNT)
        .Change(Bip44Changes.CHAIN_EXT)
        .AddressIndex(_XRPL_BIP44_ADDRESS_INDEX)
    )
    private_key = account.PrivateKey().Raw().ToHex().upper()
    public_key = account.PublicKey().RawCompressed().ToHex().upper()
    return Wallet(
        public_key=public_key,
        private_key=_SECP256K1_PRIVATE_KEY_PREFIX + private_key,
    )
