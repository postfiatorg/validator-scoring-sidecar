"""Historical reproduction and exact strict-parser source parity."""
import hashlib
import json
from pathlib import Path

import pytest

from validator_scoring_sidecar.scoring import SUPPORTED_PARSER_CONTENT_HASHES
from validator_scoring_sidecar.scoring import parser, parser_duplicate_keys
from validator_scoring_sidecar.scoring.parser_versions import (
    LEGACY_PARSER_HASH, STRICT_PARSER_HASH, parse_response_for_hash,
)
from validator_scoring_sidecar.verification import compute_verification_hashes


@pytest.mark.parametrize('raw', [
    '{"network_summary":"first","network_summary":"last"}',
    '```json\n{"network_summary":"first","network_summary":"last"}\n```',
    'prefix {"network_summary":"first","network_summary":"last"} suffix',
    '{"network_summary":"ok","x":{"a":1,"a":2}}',
    r'{"network_summary":"ok","x":1,"\u0078":2}',
])
def test_round_hash_selects_historical_or_strict_behavior(raw):
    historical = parse_response_for_hash(raw, {}, LEGACY_PARSER_HASH)
    assert historical.model_dump() == parser.parse_response(raw, {}).model_dump()
    strict = parse_response_for_hash(raw, {}, STRICT_PARSER_HASH)
    assert not strict.complete
    assert strict.validator_scores == []
    assert strict.raw_response == raw
    assert 'Duplicate JSON member:' in strict.errors[0]


@pytest.mark.parametrize('raw', [
    '{"network_summary":"healthy"}',
    'not json',
    '{"network_summary":null}',
    json.dumps({'network_summary':'healthy','v1': {
        'score':85,'consensus':95,'reliability':80,'software':80,
        'diversity':80,'identity':80,'reasoning':'stable',
    }}),
])
@pytest.mark.parametrize('formula', [False, True])
def test_nonduplicate_round_hashes_unchanged(raw, formula):
    args = dict(previous_unl=[], selector_parameters={
        'score_cutoff':40,'max_size':35,'min_score_gap':5,
    }, apply_score_formula=formula)
    mapping = {'v1':{'master_key':'MASTER'}}
    assert compute_verification_hashes(raw, mapping, **args) == compute_verification_hashes(
        raw, mapping, parser_content_hash=STRICT_PARSER_HASH, **args,
    )


@pytest.mark.parametrize('unknown', ['unknown', '', None, []])
def test_unknown_parser_fails_closed(unknown):
    with pytest.raises(ValueError, match='Unsupported parser'):
        parse_response_for_hash('{}', {}, unknown)


def test_every_supported_parser_has_exact_source_and_adaptation():
    source = Path(parser.__file__).parent / '_vendor_source'
    assert SUPPORTED_PARSER_CONTENT_HASHES == {LEGACY_PARSER_HASH, STRICT_PARSER_HASH}
    strict = (source / 'response_parser_duplicate_keys.py').read_bytes()
    assert hashlib.sha256(strict).hexdigest() == STRICT_PARSER_HASH
    assert hashlib.sha256((source / 'response_parser.py').read_bytes()).hexdigest() == LEGACY_PARSER_HASH
    assert Path(parser_duplicate_keys.__file__).read_text() == strict.decode().replace(
        'from scoring_service.services.prompt_builder import ValidatorIdentityMap',
        'ValidatorIdentityMap = dict[str, dict[str, str]]',
    )
