"""UNL selector for foundation rounds carrying the tied-incumbent fix.

Vendored from foundation ``scoring_service/services/unl_selector.py`` at
content hash ``1c665f5c...``. Local adaptations require the three manifest
parameters and import the sidecar's vendored parser result type.
"""

import logging
from dataclasses import dataclass

from validator_scoring_sidecar.scoring.parser import ScoringResult

logger = logging.getLogger(__name__)


@dataclass
class UNLSelectionResult:
    """Output of the UNL selection algorithm."""

    unl: list[str]
    alternates: list[str]


def select_unl(
    scoring_result: ScoringResult,
    *,
    cutoff: int,
    max_size: int,
    min_gap: int,
    previous_unl: list[str] | None = None,
) -> UNLSelectionResult:
    """Select validators using the foundation's tied-incumbent behavior."""
    qualified = sorted(
        [v for v in scoring_result.validator_scores if v.score >= cutoff],
        key=lambda v: (-v.score, v.master_key),
    )

    if not qualified:
        logger.warning("No validators above cutoff %d — UNL is empty", cutoff)
        return UNLSelectionResult(unl=[], alternates=[])

    is_first_round = not previous_unl
    previous_unl_set = set(previous_unl) if previous_unl else set()

    if is_first_round:
        unl_keys = [v.master_key for v in qualified[:max_size]]
        alternate_keys = [v.master_key for v in qualified[max_size:]]
    else:
        surviving_incumbents = sorted(
            [v for v in qualified if v.master_key in previous_unl_set],
            key=lambda v: (-v.score, v.master_key),
        )

        if len(surviving_incumbents) > max_size:
            capped_incumbents = surviving_incumbents[:max_size]
            cap_displaced_incumbents = surviving_incumbents[max_size:]
        else:
            capped_incumbents = surviving_incumbents
            cap_displaced_incumbents = []

        unl = list(capped_incumbents)
        challengers = [
            v for v in qualified if v.master_key not in previous_unl_set
        ]

        open_seats = max_size - len(unl)
        remaining_challengers = []

        for challenger in challengers:
            if open_seats > 0:
                unl.append(challenger)
                open_seats -= 1
                continue

            weakest = max(unl, key=lambda v: (-v.score, v.master_key))
            if challenger.score >= weakest.score + min_gap:
                unl.remove(weakest)
                unl.append(challenger)
                remaining_challengers.append(weakest)
            else:
                remaining_challengers.append(challenger)

        unl.sort(key=lambda v: (-v.score, v.master_key))

        alternates = cap_displaced_incumbents + remaining_challengers
        alternates.sort(key=lambda v: (-v.score, v.master_key))

        unl_keys = [v.master_key for v in unl]
        alternate_keys = [v.master_key for v in alternates]

    logger.info(
        "UNL selected: %d validators, %d alternates (cutoff=%d, max=%d, gap=%d, first_round=%s)",
        len(unl_keys),
        len(alternate_keys),
        cutoff,
        max_size,
        min_gap,
        is_first_round,
    )

    return UNLSelectionResult(unl=unl_keys, alternates=alternate_keys)
