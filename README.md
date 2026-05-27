# Validator Scoring Sidecar

Validator-facing tooling for Post Fiat Dynamic UNL Phase 2 shadow verification.

This repository starts the operator-owned sidecar path. The current scope is
limited to syncing verified frozen input packages that future sidecar
capabilities will score and publish through commit-reveal.

The sidecar is convenience tooling. Validators can inspect the frozen package
and reproduce the same steps manually; the tool should not become a hidden trust
requirement.

## Current Scope

The sidecar discovers recent scoring rounds, requires the frozen input package
boundary, downloads the frozen input package, verifies `bundle.json` against
`input_package_hash`, verifies every file listed in `bundle.json.file_hashes`,
rejects cross-network packages, writes only verified packages to the local
cache, and records local round state in SQLite.

- `input_package_cid`
- `input_package_hash`
- `input_frozen_at`
- `final_bundle_cid`, when present, as a separate final audit bundle reference

The sidecar does not run inference, score validators, inspect package semantics
in depth, watch chain history, submit commits or reveals, handle wallets or
validator keys, run as a daemon, report convergence, or change Validator List
authority.

Phase 2 evidence is observational. The foundation scoring service remains the
authoritative Validator List publisher while sidecars are introduced and tested.

## Install

Use Python 3.11 or newer.

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e ".[dev]"
```

## Configuration

Configuration precedence is:

```text
CLI flag > environment variable > default
```

| Setting | CLI flag | Environment variable | Default |
| --- | --- | --- | --- |
| Scoring service base URL | `--base-url` | `POSTFIAT_SCORING_BASE_URL` | Network-scoped: `https://scoring-{network}.postfiat.org` for devnet/testnet |
| Sidecar data directory | `--data-dir` | `POSTFIAT_SIDECAR_DATA_DIR` | `~/.postfiat/validator-scoring-sidecar/{network}` |
| IPFS gateway URL prefix | `--ipfs-gateway-url` | `POSTFIAT_SIDECAR_IPFS_GATEWAY_URL` | Shared: `https://ipfs-testnet.postfiat.org/ipfs` for devnet/testnet |
| Network label | `--network` | `POSTFIAT_SIDECAR_NETWORK` | `testnet` |
| Request timeout seconds | `--timeout` | `POSTFIAT_SIDECAR_TIMEOUT_SECONDS` | `30` |

When no base URL, IPFS gateway URL, or data directory is configured, the scoring
base URL and data directory are scoped by network. The default network is
testnet, so the CLI uses `https://scoring-testnet.postfiat.org`, the shared
`https://ipfs-testnet.postfiat.org/ipfs` gateway, and cache state under
`~/.postfiat/validator-scoring-sidecar/testnet`. Passing `--network devnet`
switches the scoring base URL and data directory to devnet, even if a testnet
env file is currently loaded. Devnet and testnet use the same Post Fiat IPFS
gateway because package content is content-addressed and does not conflict
between networks. Explicit `--base-url`, `--ipfs-gateway-url`, and `--data-dir`
values override everything for their settings. Environment variables override
defaults when the corresponding CLI flag is not provided.

The data directory is where verified input packages and local sidecar state are
stored. Local round state is stored in `sidecar.db`; verified packages are
stored under `packages/`.

Example environment files are provided for devnet and testnet:

```bash
cp .env.testnet.example .env
set -a
source .env
set +a
```

The sidecar reads exported environment variables. It does not parse `.env`
files directly, which keeps the runtime dependency set small. Use
`.env.devnet.example` the same way when fetching devnet scoring rounds.

You can also select devnet without an env file:

```bash
validator-scoring-sidecar sync --network devnet
```

## Sync Inputs

Run one sync pass to discover and verify the newest unhandled public round with
frozen input metadata:

```bash
validator-scoring-sidecar sync
```

The command calls `GET /api/scoring/rounds`, scans recent rounds newest first,
skips rounds that do not expose `input_package_cid`, `input_package_hash`, and
`input_frozen_at`, and reuses the same verified package fetch/cache path as
`fetch-input-package`.

By default, sync scans up to 5 recent rounds. Use `--round-limit` to scan
farther back during recovery or debugging; the sidecar caps this at 20.

Human-readable output:

```text
Sync status: input package ready
Action: fetched
Scanned rounds: 3
Round ID: 123
Round number: 123
Network: testnet
Input package CID: Qm...
Input package hash: 0123...
Input frozen at: 2026-05-25T00:00:00+00:00
Source: https
Cache status: fetched
Verified files: 9
Local path: /home/validator/.postfiat/validator-scoring-sidecar/testnet/packages/0123...
```

Machine-readable output:

```bash
validator-scoring-sidecar sync --json
```

```json
{
  "action": "fetched",
  "scanned_rounds": 3,
  "network": "testnet",
  "package": {
    "cached": false,
    "input_frozen_at": "2026-05-25T00:00:00+00:00",
    "input_package_cid": "Qm...",
    "input_package_hash": "0123...",
    "local_path": "/home/validator/.postfiat/validator-scoring-sidecar/testnet/packages/0123...",
    "network": "testnet",
    "round_id": 123,
    "round_number": 123,
    "source": "https",
    "verified_file_count": 9
  },
  "round_id": 123,
  "round_number": 123,
  "status": "input_package_ready"
}
```

If no recent unhandled round exposes frozen input metadata, sync exits `0` with
`no_eligible_round` status. Sync uses a local advisory lock under the data
directory so overlapping cron or manual runs do not mutate the same state at
the same time.

If sync reports that a previously verified package cache failed verification,
repair it by refetching the known round directly:

```bash
validator-scoring-sidecar fetch-input-package --round-id 123 --force
```

## Fetch An Input Package

Fetch, verify, and cache a frozen input package for a known public round. The
command first calls `GET /api/scoring/rounds/{round_id}` to discover the
round's frozen input package metadata. `round_id` is the scoring service
database ID. The response also includes the public `round_number`.

If a round does not expose `input_package_cid`, `input_package_hash`, or
`input_frozen_at`, the command exits nonzero and reports:

```text
Round 123 does not expose frozen input package metadata (input_package_cid, input_package_hash, input_frozen_at). It may be a legacy, dry-run, override, or round created before frozen input metadata was introduced.
```

The round may still be valid historical audit data. It is just not suitable for
frozen input inspection.

```bash
validator-scoring-sidecar fetch-input-package --round-id 123
```

The default source is automatic. The sidecar tries the scoring-service HTTPS
fallback first, then falls back to the configured IPFS gateway. Operators can
force one source when needed:

```bash
validator-scoring-sidecar fetch-input-package --round-id 123 --source https
validator-scoring-sidecar fetch-input-package --round-id 123 --source ipfs
```

Human-readable output:

```text
Round ID: 123
Round number: 123
Network: testnet
Input package CID: Qm...
Input package hash: 0123...
Input frozen at: 2026-05-25T00:00:00+00:00
Source: https
Cache status: fetched
Verified files: 9
Local path: /home/validator/.postfiat/validator-scoring-sidecar/testnet/packages/0123...
```

Machine-readable output:

```bash
validator-scoring-sidecar fetch-input-package --round-id 123 --json
```

```json
{
  "cached": false,
  "input_frozen_at": "2026-05-25T00:00:00+00:00",
  "input_package_cid": "Qm...",
  "input_package_hash": "0123...",
  "local_path": "/home/validator/.postfiat/validator-scoring-sidecar/testnet/packages/0123...",
  "network": "testnet",
  "round_id": 123,
  "round_number": 123,
  "source": "https",
  "verified_file_count": 9
}
```

Verified packages are cached by `input_package_hash`:

```text
~/.postfiat/validator-scoring-sidecar/testnet/packages/<input_package_hash>/
```

The sidecar also writes local metadata under `.sidecar/package.json` inside the
cache directory. That file records the round, network, package CID, package
hash, freeze timestamp, fetch source, fetch timestamp, and verification summary.
It is local sidecar state, not part of the official frozen input package.

By default, an existing verified cache is reused. Pass `--force` to refetch and
replace it:

```bash
validator-scoring-sidecar fetch-input-package --round-id 123 --force
```

## Exit Codes

| Code | Meaning |
| --- | --- |
| `0` | Command completed successfully, including sync no-op cases. |
| `1` | Expected operator-facing failure, such as missing frozen input metadata or package verification failure. |
| `2` | CLI usage or configuration error. |
| `3` | Network, HTTP, malformed metadata, or response decoding error from the scoring service or package source. |
| `4` | Sync lock was already held by another sidecar run. |

## Development

For a shorter operator/developer command reference, see
[`docs/Usage.md`](docs/Usage.md).

Run tests without live service calls:

```bash
python -m pytest
```

The tests use mocked HTTP and cover config precedence, round URL construction,
round discovery, round metadata parsing, missing frozen-input metadata behavior,
verified package fetching, cache behavior, source selection, SQLite state,
local locking, and CLI output modes.
