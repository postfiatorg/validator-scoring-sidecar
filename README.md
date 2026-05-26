# Validator Scoring Sidecar

Validator-facing tooling for Post Fiat Dynamic UNL Phase 2 shadow verification.

This repository starts the operator-owned sidecar path. The current milestone is
limited to inspecting public scoring round metadata and reporting the frozen
input package fields that later sidecar milestones will download, verify, score,
and publish through commit-reveal.

The sidecar is convenience tooling. Validators can inspect the frozen package
and reproduce the same steps manually; the tool should not become a hidden trust
requirement.

## Current Scope

The first command reads a scoring service round record and reports the frozen
input package boundary:

- `input_package_cid`
- `input_package_hash`
- `input_frozen_at`
- `final_bundle_cid`, when present, as a separate final audit bundle reference

The command does not download the package, verify hashes, run inference, watch
chain history, submit commits or reveals, handle wallets or validator keys, or
change Validator List authority.

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
| Network label | `--network` | `POSTFIAT_SIDECAR_NETWORK` | `testnet` |
| Request timeout seconds | `--timeout` | `POSTFIAT_SIDECAR_TIMEOUT_SECONDS` | `30` |

When no base URL or data directory is configured, both defaults are scoped by
network. The default network is testnet, so the CLI uses
`https://scoring-testnet.postfiat.org` and
`~/.postfiat/validator-scoring-sidecar/testnet`. Passing `--network devnet`
switches those defaults to devnet, even if a testnet env file is currently
loaded. Explicit `--base-url` and `--data-dir` values override everything for
their settings. `POSTFIAT_SCORING_BASE_URL` and `POSTFIAT_SIDECAR_DATA_DIR`
override defaults when the corresponding CLI flag is not provided.

The data directory is where future sidecar commands will cache verified input
packages and local state. This milestone defines the location but does not
download package files yet.

Example environment files are provided for devnet and testnet:

```bash
cp .env.testnet.example .env
set -a
source .env
set +a
```

The sidecar reads exported environment variables. It does not parse `.env`
files directly, which keeps the runtime dependency set small. Use
`.env.devnet.example` the same way when inspecting devnet scoring rounds.

You can also select devnet without an env file:

```bash
validator-scoring-sidecar inspect-round --network devnet --round-id 123
```

## Inspect A Round

The scoring service endpoint is:

```text
GET /api/scoring/rounds/{round_id}
```

`round_id` is the scoring service database ID. The response also includes the
public `round_number`.

Human-readable output:

```bash
validator-scoring-sidecar inspect-round --round-id 123
```

```text
Round ID: 123
Round number: 123
Status: COMPLETE
Input package CID: Qm...
Input package hash: 0123...
Input frozen at: 2026-05-25T00:00:00+00:00
Final bundle CID: Qm...
```

Machine-readable output:

```bash
validator-scoring-sidecar inspect-round --round-id 123 --json
```

```json
{
  "final_bundle_cid": "Qm...",
  "input_frozen_at": "2026-05-25T00:00:00+00:00",
  "input_package_cid": "Qm...",
  "input_package_hash": "0123...",
  "round_id": 123,
  "round_number": 123,
  "status": "COMPLETE"
}
```

If a round does not expose `input_package_cid`, `input_package_hash`, or
`input_frozen_at`, the command exits nonzero and reports:

```text
Round 123 does not expose frozen input package metadata (input_package_cid, input_package_hash, input_frozen_at). It may be a legacy, dry-run, override, or pre-M2.1 round.
```

The round may still be valid historical audit data. It is just not suitable for
frozen input inspection in this milestone.

## Exit Codes

| Code | Meaning |
| --- | --- |
| `0` | Frozen input metadata was found and printed. |
| `1` | Expected operator-facing failure, such as missing frozen input metadata. |
| `2` | CLI usage or configuration error. |
| `3` | Network, HTTP, malformed metadata, or response decoding error from the scoring service. |

## Development

Run tests without live service calls:

```bash
python -m pytest
```

The tests use mocked HTTP and cover config precedence, round URL construction,
round metadata parsing, missing frozen-input metadata behavior, and CLI output
modes.
