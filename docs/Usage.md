# Usage

The validator-scoring-sidecar runs alongside your Post Fiat Ledger validator. It fetches and verifies the foundation's frozen input package for each scoring round and caches the result locally. This guide walks you through running it on devnet or testnet.

## What the sidecar does

- Discovers recent scoring rounds from the public Post Fiat scoring service.
- Downloads the foundation's frozen input package for the latest unhandled round.
- Verifies the package's `bundle.json` against `input_package_hash` and verifies every listed file using the canonical JSON hash rule.
- Caches verified packages and records local round state in SQLite.

## What the sidecar does NOT do

The sidecar is convenience tooling, not a trust requirement. The `docker compose` workflow above runs only the unattended input sync. Independent inference and scoring are an opt-in, host-run step — see [`Deployment.md`](Deployment.md) for standing up an inference runtime, after which `score` reproduces a round and records the outcome.

The sidecar can also participate in the foundation's on-chain commit-reveal protocol: it watches the foundation publisher account for round announcements and, inside the announced windows, submits your validator's salted output commitment and then its reveal on PFTL. This is opt-in and all-or-nothing — set `POSTFIAT_SIDECAR_MODE=participate` and supply a funded operator relay wallet seed (`POSTFIAT_SIDECAR_VALIDATOR_WALLET_SEED`), validator-keys access (`POSTFIAT_SIDECAR_VALIDATOR_KEYS_PATH`), a reachable PFTL RPC, and a discoverable foundation publisher address; if anything is missing the command fails fast and changes nothing on chain. The default mode (`sync`) keeps running input verification only.

Even when participating, the sidecar does **not** hold your validator master key. Commit and reveal authorship is signed through the postfiatd `validator-keys` tool, and the on-chain transaction is paid for and submitted by a **separate funded operator wallet** — an ordinary `r...` PFTL address whose seed you supply — never your validator identity. The sidecar still does not sign or publish Validator Lists, publish convergence reports, or change Validator List authority.

## Setup

You need a host with Docker and Docker Compose. There is nothing to clone or build: the sidecar ships as published Docker images (`agtipft/validator-scoring-sidecar`), built and gate-checked by CI from the repository's environment branches. You only download the compose files and an environment template.

Create a directory for the deployment:

```bash
mkdir validator-scoring-sidecar && cd validator-scoring-sidecar
```

Then pick a network and fetch the three files from the matching branch. Testnet:

```bash
curl -fsSLO https://raw.githubusercontent.com/postfiatorg/validator-scoring-sidecar/testnet/docker-compose.yml
curl -fsSLO https://raw.githubusercontent.com/postfiatorg/validator-scoring-sidecar/testnet/docker-compose.participate.yml
curl -fsSL https://raw.githubusercontent.com/postfiatorg/validator-scoring-sidecar/testnet/.env.testnet.example -o .env
```

Or devnet:

```bash
curl -fsSLO https://raw.githubusercontent.com/postfiatorg/validator-scoring-sidecar/devnet/docker-compose.yml
curl -fsSLO https://raw.githubusercontent.com/postfiatorg/validator-scoring-sidecar/devnet/docker-compose.participate.yml
curl -fsSL https://raw.githubusercontent.com/postfiatorg/validator-scoring-sidecar/devnet/.env.devnet.example -o .env
```

Start the sidecar:

```bash
docker compose up -d
```

That's it. Docker pulls the published image for your network (the compose file selects the tag from `POSTFIAT_SIDECAR_NETWORK` in `.env`), runs the sync loop in the background, and persists verified packages plus state under a Docker named volume.

## Updating the sidecar

```bash
docker compose pull
docker compose up -d
```

Pulling fetches the latest published image for your network; restarting picks it up. Data and state in the named volume are untouched. Add the participation overlay to both commands if you run participation mode.

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

## Participation mode

The default deployment runs verify-only sync. To run the full on-chain commit-reveal loop, deploy with the participation overlay on top of the base compose file:

```bash
docker compose -f docker-compose.yml -f docker-compose.participate.yml up -d
```

The overlay switches to the published participation image, which bundles the postfiatd `validator-keys` signing tool — sourced from your environment's published postfiatd image and executed during the publish build, so an incompatible binary fails the release instead of a live round. It also mounts your validator key file read-only into the container.

Before starting, uncomment the participation block in your `.env`: set `POSTFIAT_SIDECAR_MODE=participate`, the funded relay wallet seed, and `POSTFIAT_SIDECAR_VALIDATOR_KEYS_FILE` pointing at your `validator-keys.json` on the host. Participation is all-or-nothing: if any prerequisite is missing, the container logs a clear error and changes nothing on chain. The verify-only deployment is unaffected by the overlay's existence — `docker compose up -d` without the overlay keeps pulling and running the sync-only image.

Participation also needs an inference runtime (see [`Deployment.md`](Deployment.md)) — scoring runs there, and a pass that cannot score has nothing to commit. For the full list of participation variables and the key-handling model, see [`Configuration.md`](Configuration.md).

## Configuration

The setup above does not require editing any environment values. For the full list of variables, including advanced tunables you should normally leave at their defaults, see [`Configuration.md`](Configuration.md).
