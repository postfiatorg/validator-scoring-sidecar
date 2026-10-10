"""Historical and minimum-cap selector compatibility."""
import ast
import hashlib
from pathlib import Path

import pytest

from validator_scoring_sidecar.scoring import SUPPORTED_SELECTOR_CONTENT_HASHES
from validator_scoring_sidecar.scoring import (
    selector,
    selector_minimum_cap,
    selector_minimum_cap_tie_break,
    selector_tie_break,
)
from validator_scoring_sidecar.scoring.parser import ScoringResult, ValidatorScore
from validator_scoring_sidecar.scoring.selector_versions import (
    COMBINED_SELECTOR_HASH, LEGACY_SELECTOR_HASH, STRICT_SELECTOR_HASH,
    TIE_BREAK_SELECTOR_HASH,
    select_unl_for_hash,
)


def result(populated):
    scores = [ValidatorScore(master_key='MASTER', score=90, consensus=90,
        reliability=90, software=90, diversity=90, identity=90, reasoning='ok')] if populated else []
    return ScoringResult(validator_scores=scores, network_summary='ok',
        network_report=None, raw_response='{}', complete=True, errors=[])


@pytest.mark.parametrize('cap', [0, -1])
@pytest.mark.parametrize('populated', [False, True])
@pytest.mark.parametrize('previous', [[], ['OLD']])
def test_strict_boundary_is_selected_by_hash(cap, populated, previous):
    with pytest.raises(ValueError, match='max_size must be at least 1'):
        select_unl_for_hash(result(populated), STRICT_SELECTOR_HASH,
            cutoff=40, max_size=cap, min_gap=5, previous_unl=previous)
    with pytest.raises(ValueError, match='max_size must be at least 1'):
        select_unl_for_hash(result(populated), COMBINED_SELECTOR_HASH,
            cutoff=40, max_size=cap, min_gap=5, previous_unl=previous)


@pytest.mark.parametrize('cap', [0, -1, 1, 35])
@pytest.mark.parametrize('populated', [False, True])
def test_legacy_version_preserves_historical_outputs(cap, populated):
    args = dict(cutoff=40, max_size=cap, min_gap=5, previous_unl=[])
    actual = select_unl_for_hash(result(populated), LEGACY_SELECTOR_HASH, **args)
    expected = selector.select_unl(result(populated), **args)
    assert actual == expected


@pytest.mark.parametrize('cap', [1, 35])
@pytest.mark.parametrize('previous', [[], ['MASTER'], ['OLD']])
def test_valid_caps_preserve_selection(cap, previous):
    args = dict(cutoff=40, max_size=cap, min_gap=5, previous_unl=previous)
    old = select_unl_for_hash(result(True), LEGACY_SELECTOR_HASH, **args)
    new = select_unl_for_hash(result(True), STRICT_SELECTOR_HASH, **args)
    assert (old.unl, old.alternates) == (new.unl, new.alternates)


@pytest.mark.parametrize('unknown', ['', 'unknown', None, []])
def test_unknown_hash_fails_closed(unknown):
    with pytest.raises(ValueError, match='Unsupported selector'):
        select_unl_for_hash(result(False), unknown, cutoff=40, max_size=35, min_gap=5)


def test_tie_break_version_displaces_lexicographically_larger_weakest_incumbent():
    scores = [
        ValidatorScore(master_key=key, score=score, consensus=score,
            reliability=score, software=score, diversity=score, identity=score,
            reasoning='ok')
        for key, score in [('A', 50), ('B', 50), ('C', 55)]
    ]
    scored = ScoringResult(validator_scores=scores, network_summary='ok',
        network_report=None, raw_response='{}', complete=True, errors=[])
    args = dict(cutoff=40, max_size=2, min_gap=5, previous_unl=['A', 'B'])
    legacy = select_unl_for_hash(scored, LEGACY_SELECTOR_HASH, **args)
    fixed = select_unl_for_hash(scored, TIE_BREAK_SELECTOR_HASH, **args)
    combined = select_unl_for_hash(scored, COMBINED_SELECTOR_HASH, **args)
    assert legacy.unl == ['C', 'B']
    assert fixed.unl == ['C', 'A']
    assert combined.unl == ['C', 'A']


def test_source_provenance_and_runnable_body_parity():
    source = Path(selector.__file__).parent / '_vendor_source'
    raw = (source / 'unl_selector_minimum_cap.py').read_bytes()
    assert hashlib.sha256(raw).hexdigest() == STRICT_SELECTOR_HASH
    assert hashlib.sha256((source / 'unl_selector.py').read_bytes()).hexdigest() == LEGACY_SELECTOR_HASH
    tie_raw = (source / 'unl_selector_tie_break.py').read_bytes()
    assert hashlib.sha256(tie_raw).hexdigest() == TIE_BREAK_SELECTOR_HASH
    combined_raw = (source / 'unl_selector_minimum_cap_tie_break.py').read_bytes()
    assert hashlib.sha256(combined_raw).hexdigest() == COMBINED_SELECTOR_HASH
    assert SUPPORTED_SELECTOR_CONTENT_HASHES == {
        COMBINED_SELECTOR_HASH, LEGACY_SELECTOR_HASH, STRICT_SELECTOR_HASH,
        TIE_BREAK_SELECTOR_HASH,
    }
    upstream = next(n for n in ast.parse(raw).body if isinstance(n, ast.FunctionDef) and n.name == 'select_unl')
    adapted = next(n for n in ast.parse(Path(selector_minimum_cap.__file__).read_text()).body if isinstance(n, ast.FunctionDef) and n.name == 'select_unl')
    # Only the docstring and three settings fallback assignments are removed
    # from the upstream body; selection/validation must otherwise be identical.
    assert [ast.dump(n) for n in upstream.body[4:]] == [ast.dump(n) for n in adapted.body[1:]]
    tie_upstream = next(n for n in ast.parse(tie_raw).body if isinstance(n, ast.FunctionDef) and n.name == 'select_unl')
    tie_adapted = next(n for n in ast.parse(Path(selector_tie_break.__file__).read_text()).body if isinstance(n, ast.FunctionDef) and n.name == 'select_unl')
    assert [ast.dump(n) for n in tie_upstream.body[4:]] == [ast.dump(n) for n in tie_adapted.body[1:]]
    combined_upstream = next(n for n in ast.parse(combined_raw).body if isinstance(n, ast.FunctionDef) and n.name == 'select_unl')
    combined_adapted = next(n for n in ast.parse(Path(selector_minimum_cap_tie_break.__file__).read_text()).body if isinstance(n, ast.FunctionDef) and n.name == 'select_unl')
    assert [ast.dump(n) for n in combined_upstream.body[4:]] == [ast.dump(n) for n in combined_adapted.body[1:]]
