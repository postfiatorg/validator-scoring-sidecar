# Validator Scoring Sidecar

Validator-facing tooling for the Post Fiat Dynamic UNL shadow verification path. The sidecar fetches, verifies, and caches frozen input packages from the foundation scoring service on a Post Fiat Ledger validator host.

This file is for developers working on the sidecar source. Validator operators deploying the sidecar should read [`docs/Usage.md`](docs/Usage.md) and [`docs/Configuration.md`](docs/Configuration.md) instead.

## Repository layout

```
src/validator_scoring_sidecar/             Sidecar source: CLI, config, fetch + verify, SQLite state
src/validator_scoring_sidecar/scoring/     Vendored foundation parser and selector
tests/                                     pytest suite
scripts/                                   Maintainer scripts (vendor freshness)
docs/                                      Operator documentation
.github/workflows/                         CI: vendor freshness against foundation main/devnet/testnet
Dockerfile, docker-compose.yml, entrypoint.sh    Operator deployment packaging
.env.testnet.example, .env.devnet.example        Per-network operator env templates
```

## Local development

Requires Python 3.11 or newer.

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e ".[dev]"
```

## Running the tests

```bash
python -m pytest
```

Tests are mocked at the HTTP layer — nothing reaches the live scoring service or IPFS gateway.

## Running the CLI directly

The Docker container invokes the `validator-scoring-sidecar` console script that this package installs. While developing or debugging you can run the same CLI directly:

```bash
validator-scoring-sidecar sync
validator-scoring-sidecar fetch-input-package --round-id <id>
```

Direct CLI use is a development convenience. The operator deployment path is Docker Compose; the operator docs do not present the CLI as a parallel install path.

## Vendor freshness

The `validator_scoring_sidecar.scoring` sub-package vendors the foundation parser and selector at pinned content hashes. To check whether the live foundation source still matches the vendored copy:

```bash
python scripts/check_vendor_freshness.py --branch main --mode warning
```

`.github/workflows/vendor-freshness.yml` runs this automatically on every push and pull request against `main`, `devnet`, and `testnet`.
