# Usage

The validator-scoring-sidecar runs alongside your Post Fiat Ledger validator. It fetches and verifies the foundation's frozen input package for each scoring round and caches the result locally. This guide walks you through running it on devnet or testnet.

## What the sidecar does

- Discovers recent scoring rounds from the public Post Fiat scoring service.
- Downloads the foundation's frozen input package for the latest unhandled round.
- Verifies the package's `bundle.json` against `input_package_hash` and verifies every listed file using the canonical JSON hash rule.
- Caches verified packages and records local round state in SQLite.

## What the sidecar does NOT do

The sidecar is convenience tooling, not a trust requirement. The `docker compose` workflow above runs only the unattended input sync. Independent inference and scoring are an opt-in, host-run step — see [`Deployment.md`](Deployment.md) for standing up an inference runtime, after which `score` reproduces a round and records the outcome.

A later release will let the sidecar participate in the on-chain commit-reveal protocol: watching the foundation publisher account for round announcements and submitting your validator's salted output commitment on PFTL. Even then, the sidecar does **not** hold your validator master key. Commit authorship is signed through the postfiatd `validator-keys` tool, and the on-chain transaction is paid for and submitted by a **separate funded operator wallet** — an ordinary `r...` PFTL address whose seed you supply — never your validator identity. The sidecar still does not sign or publish Validator Lists, publish convergence reports, or change Validator List authority.

## Setup

You need a host with Docker and Docker Compose.

Clone the repository:

```bash
git clone https://github.com/postfiatorg/validator-scoring-sidecar.git
cd validator-scoring-sidecar
```

Pick a network. Testnet:

```bash
cp .env.testnet.example .env
```

Or devnet:

```bash
cp .env.devnet.example .env
```

Start the sidecar:

```bash
docker compose up -d
```

That's it. The container runs the sync loop in the background and persists verified packages plus state under a Docker named volume.

## Verifying a healthy first sync

Watch the logs:

```bash
docker compose logs -f sidecar
```

You should see lines like:

```
2026-05-29T00:00:00+00:00 validator-scoring-sidecar: starting sync loop (interval=3600s)
2026-05-29T00:00:02+00:00 validator-scoring-sidecar: sync completed; sleeping 3600s
```

A `sync completed` line means the first pass succeeded. Either it fetched a fresh round, or the scoring service had no eligible round to expose right now — both outcomes are normal. Foundation rounds happen on a weekly cadence.

## One-shot commands

To fetch a specific round by its scoring-service ID (for example to test or to recover a known-bad cache entry):

```bash
docker compose run --rm sidecar fetch-input-package --round-id 268
```

Add `--force` to refetch and replace an existing cached package.

## Recovering from a corrupt cache

If `sync` logs that a previously verified package failed verification, refetch the round directly with `--force`:

```bash
docker compose run --rm sidecar fetch-input-package --round-id <id> --force
```

## Stopping the sidecar

```bash
docker compose down
```

This stops and removes the container. The named volume holding verified packages and SQLite state remains, so a later `docker compose up -d` resumes where you left off. To remove the data too, pass `-v`:

```bash
docker compose down -v
```

## Switching networks

Stop the stack, copy the other env example, and start again:

```bash
docker compose down
cp .env.devnet.example .env   # or .env.testnet.example
docker compose up -d
```

The data volume retains state from the previous network. If you want a clean slate, also pass `-v` to `docker compose down`.

## Configuration

The setup above does not require editing any environment values. For the full list of variables, including advanced tunables you should normally leave at their defaults, see [`Configuration.md`](Configuration.md).
