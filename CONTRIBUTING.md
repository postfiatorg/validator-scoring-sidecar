# Contributing

## Before You Start

Open an issue first and wait for a maintainer go-ahead before writing code. This keeps effort on changes the project actually needs and avoids duplicate work.

Defensive or hardening changes are accepted only with evidence that the bad input actually occurs in a deployed environment: a log excerpt, a captured response, or a failed round. Guards against inputs that have never been observed are closed.

## Vendored Scoring Modules

The `validator_scoring_sidecar.scoring` package vendors the foundation parser, selector, score formula, diversity formula and commit-reveal modules and identifies them by content hash. These copies change only through the refresh procedure documented in that package, in step with a foundation release. Do not add alternative implementations or hashes for code that is not merged in `postfiatorg/dynamic-unl-scoring`.

## Pull Requests

- Link the approved issue in the PR description
- Run `pytest` and `ruff check src tests` and fix any failures
- Keep PRs focused: one fix or feature per PR
