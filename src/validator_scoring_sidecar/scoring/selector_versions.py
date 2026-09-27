"""Select the implementation pinned by each round, preserving historical behavior."""
from validator_scoring_sidecar.scoring import selector, selector_minimum_cap

LEGACY_SELECTOR_HASH = "cdd65a60565ba5ac340b5be60421f770905fc461cefa770c71465a179c2ff9f2"
STRICT_SELECTOR_HASH = "a3b9db038e6583b7d34d19b6e1ecf979bcb98f9e91404d19d2ed240791750d16"
SELECTORS = {
    LEGACY_SELECTOR_HASH: selector.select_unl,
    STRICT_SELECTOR_HASH: selector_minimum_cap.select_unl,
}


def select_unl_for_hash(scoring_result, content_hash, **parameters):
    try:
        select = SELECTORS[content_hash]
    except (KeyError, TypeError):
        raise ValueError(f"Unsupported selector content hash: {content_hash!r}") from None
    return select(scoring_result, **parameters)
