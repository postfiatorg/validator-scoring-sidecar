# Usage

This sidecar currently has one main command:

- fetch, verify, and cache a frozen input package for a known round.

It does not score validators, run inference, watch chain activity, submit memos,
or handle validator keys.

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e ".[dev]"
```

## Choose A Network

Testnet is the default:

```bash
validator-scoring-sidecar fetch-input-package --round-id 123
```

Use devnet explicitly when needed:

```bash
validator-scoring-sidecar fetch-input-package --round-id 268 --network devnet
```

You can also load an env file:

```bash
cp .env.devnet.example .env
set -a
source .env
set +a
```

Devnet and testnet both use the shared IPFS gateway:

```text
https://ipfs-testnet.postfiat.org/ipfs
```

## Fetch An Input Package

Use a throwaway data directory while testing locally:

```bash
validator-scoring-sidecar fetch-input-package \
  --round-id 268 \
  --network devnet \
  --data-dir /tmp/validator-sidecar-smoke
```

The default fetch mode tries HTTPS first and then IPFS if HTTPS fails.

Force HTTPS:

```bash
validator-scoring-sidecar fetch-input-package \
  --round-id 268 \
  --network devnet \
  --data-dir /tmp/validator-sidecar-smoke \
  --source https
```

Force IPFS:

```bash
validator-scoring-sidecar fetch-input-package \
  --round-id 268 \
  --network devnet \
  --data-dir /tmp/validator-sidecar-smoke-ipfs \
  --source ipfs
```

JSON output:

```bash
validator-scoring-sidecar fetch-input-package \
  --round-id 268 \
  --network devnet \
  --data-dir /tmp/validator-sidecar-smoke \
  --json
```

## Cache Location

You do not need to pass `--data-dir` for normal use. If it is omitted, the
sidecar uses:

```text
~/.postfiat/validator-scoring-sidecar/{network}
```

Examples:

```text
~/.postfiat/validator-scoring-sidecar/testnet
~/.postfiat/validator-scoring-sidecar/devnet
```

The `/tmp/...` paths in this guide are only for local smoke tests where you do
not want to write into your real sidecar cache.

Verified packages are stored by input package hash:

```text
{data_dir}/packages/{input_package_hash}/
```

The sidecar also writes local metadata:

```text
{data_dir}/packages/{input_package_hash}/.sidecar/package.json
```

Use `--force` to refetch and replace an existing verified cache:

```bash
validator-scoring-sidecar fetch-input-package \
  --round-id 268 \
  --network devnet \
  --data-dir /tmp/validator-sidecar-smoke \
  --force
```

## Development Checks

Run tests:

```bash
python -m pytest
```

Show CLI help:

```bash
validator-scoring-sidecar --help
validator-scoring-sidecar fetch-input-package --help
```
