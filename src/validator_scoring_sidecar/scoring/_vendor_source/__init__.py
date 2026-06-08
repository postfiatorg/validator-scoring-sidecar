"""Unadapted foundation source files retained for hash provenance.

The files in this directory are byte-identical copies of the foundation's
``scoring_service/services/response_parser.py``,
``scoring_service/services/unl_selector.py``, and
``scoring_service/services/commit_reveal.py`` at the commits the sidecar
vendored from. They exist so the values of ``SUPPORTED_PARSER_CONTENT_HASHES``,
``SUPPORTED_SELECTOR_CONTENT_HASHES``, and
``SUPPORTED_COMMIT_REVEAL_CONTENT_HASHES`` in the parent package are auditable
rather than maintainer-asserted: anyone can recompute sha256 over these files
and confirm the declared constants match.

``response_parser.py`` and ``unl_selector.py`` must not be imported at runtime:
they contain foundation imports (``scoring_service.config.settings``,
``scoring_service.services.prompt_builder``) that would fail in the sidecar
environment, so their adapted, runnable copies live in
``validator_scoring_sidecar.scoring.parser`` and
``validator_scoring_sidecar.scoring.selector`` instead.

``commit_reveal.py`` is the exception: it imports only the standard library and
``xrpl.core``, needs no adaptation, and so serves as both the provenance copy
and the runtime module. It is re-exported from the parent package as
``validator_scoring_sidecar.scoring.commit_reveal``.
"""
