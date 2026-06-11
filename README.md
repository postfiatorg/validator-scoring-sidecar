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
.github/workflows/                         CI (pytest, ruff, image build, vendor freshness) and image publishing
Dockerfile, docker-compose.yml, entrypoint.sh    Operator deployment packaging (verify-only default)
docker-compose.participate.yml                   Opt-in overlay for the on-chain participation image
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

## Linting

```bash
ruff check .
```

CI enforces this on every push and pull request, together with the test suite and a build of the verify-only Docker image. The vendored foundation modules under `scoring/_vendor_source` are excluded from linting — they are pinned by content hash and must match the foundation source byte for byte.

## Running the CLI directly

The Docker container invokes the `validator-scoring-sidecar` console script that this package installs. While developing or debugging you can run the same CLI directly:

```bash
validator-scoring-sidecar sync
validator-scoring-sidecar fetch-input-package --round-id <id>
```

Direct CLI use is a development convenience. The operator deployment path is Docker Compose; the operator docs do not present the CLI as a parallel install path.

## Releases and Docker images

Operators never build from source: pushing to the `devnet` or `testnet` environment branch runs `.github/workflows/publish.yml`, which gates on pytest and a blocking vendor-freshness check, then builds and pushes that environment's images to Docker Hub:

| Image | Contents |
|---|---|
| `agtipft/validator-scoring-sidecar:<env>-latest` | Verify-only sync image |
| `agtipft/validator-scoring-sidecar:<env>-participate-latest` | Participation image bundling the postfiatd `validator-keys` tool from `agtipft/postfiatd:<env>-light-latest`, executed during the build as a compatibility gate |

Each push also publishes immutable `<env>-<short-sha>` and `<env>-participate-<short-sha>` tags so a bad release can be rolled back by pinning. The operator compose files select the image from `POSTFIAT_SIDECAR_NETWORK` in `.env`.

To build images from source while developing:

```bash
docker build --target runtime .
docker build --platform linux/amd64 --target participate \
  --build-arg VALIDATOR_KEYS_IMAGE=agtipft/postfiatd:devnet-light-latest .
```

Only the participation build pins a platform: the postfiatd image supplying the signing tool is published for amd64 only, while the verify-only image builds on any architecture.

## Vendor freshness

The `validator_scoring_sidecar.scoring` sub-package vendors the foundation parser and selector at pinned content hashes. To check whether the live foundation source still matches the vendored copy:

```bash
python scripts/check_vendor_freshness.py --branch main --mode warning
```

`.github/workflows/vendor-freshness.yml` runs this automatically on every push and pull request against `main`, `devnet`, and `testnet`.
