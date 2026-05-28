"""Unadapted foundation source files retained for hash provenance.

The files in this directory are byte-identical copies of the foundation's
``scoring_service/services/response_parser.py`` and
``scoring_service/services/unl_selector.py`` at the commits the sidecar
vendored from. They exist so the values of ``SUPPORTED_PARSER_CONTENT_HASHES``
and ``SUPPORTED_SELECTOR_CONTENT_HASHES`` in the parent package are auditable
rather than maintainer-asserted: anyone can recompute sha256 over these files
and confirm the declared constants match.

Do not import the modules in this directory at runtime. They contain
foundation imports (``scoring_service.config.settings``,
``scoring_service.services.prompt_builder``) that would fail in the sidecar
environment. The adapted, runnable copies live in
``validator_scoring_sidecar.scoring.parser`` and
``validator_scoring_sidecar.scoring.selector`` instead.
"""
