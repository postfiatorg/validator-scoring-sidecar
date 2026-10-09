"""Dispatch parser behavior by the source hash frozen in each round.

The legacy default preserves callers and historical rounds. The strict parser
is the adapted source from dynamic-unl-scoring PR57 at f0d64987116c92e06effcb218864cf2da08256d9.
Unknown hashes fail closed, including callers outside the manifest gate.
"""

from validator_scoring_sidecar.scoring import parser, parser_duplicate_keys

LEGACY_PARSER_HASH = "1eeeed7bee91d2e6e95039018074c5e30ba3e92dffaa16257e6e5dbd07a2f7f7"
STRICT_PARSER_HASH = "677a93a077f2d27fa290e2ec8aedcfdae905c257d838f881446ecf8b7f22998b"
PARSERS = {
    LEGACY_PARSER_HASH: parser.parse_response,
    STRICT_PARSER_HASH: parser_duplicate_keys.parse_response,
}


def parse_response_for_hash(raw_text, validator_id_map, content_hash):
    try:
        parse = PARSERS[content_hash]
    except (KeyError, TypeError):
        raise ValueError(f"Unsupported parser content hash: {content_hash!r}") from None
    return parse(raw_text, validator_id_map)
