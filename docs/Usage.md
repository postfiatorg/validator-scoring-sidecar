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

Participation also needs an inference runtime — scoring runs there, and a pass that cannot score has nothing to commit. With Modal this is zero-touch: set the four Modal values in `.env` and the sidecar deploys the foundation-pinned runtime itself, and redeploys when the foundation pins a new one. Running your own local SGLang H100 instead stays operator-managed — see [`Deployment.md`](Deployment.md). For the full list of participation variables and the key-handling model, see [`Configuration.md`](Configuration.md).

## Participation lifecycle and recovery

Once participation mode is configured and the container is up, there is nothing to do per round. The loop runs one `participate` pass at the chain-poll cadence (`POSTFIAT_SIDECAR_CHAIN_POLL_INTERVAL_SECONDS`, default 60s). Each pass scores the latest eligible round, watches the foundation publisher account for that round's on-chain announcement, and — inside the announced windows — submits your commit and, on a later pass, your reveal. You "join" a round simply by running while its windows are open. Each pass logs `participate completed`, or `participate failed; sleeping ...` on an infrastructure error that is retried next pass.

### Round states

The sidecar records one row per round in its local SQLite store (`/data/sidecar.db` in the named volume, schema v5). A round advances through:

```
DISCOVERED → INPUT_PACKAGE_VERIFIED → SCORED → COMMITTED → REVEALED
```

- `DISCOVERED` — round seen; input package not yet verified.
- `INPUT_PACKAGE_VERIFIED` — frozen input package downloaded and hash-verified.
- `SCORED` — reproduced the round on your runtime and recorded your three output fingerprints (model response, validator scores, selected UNL). The foundation comparison may still be pending.
- `COMMITTED` — your salted commitment is on chain (`commit_tx_hash` recorded), with the salt and reveal windows persisted so a later pass can reopen it.
- `REVEALED` — your reveal is on chain (`reveal_tx_hash` recorded). This is the terminal happy path.

Two states sit off that ladder: `SCORING_FAILED` (scoring could not complete — inference error, runtime unavailable, and similar) and `SKIPPED` (the round will not be scored or committed — a dry-run/override round, an unsupported manifest, or a low-balance commit skip).

A foundation-comparison divergence is recorded as an annotation (`error_category` = `OUTPUT_DIVERGENCE`), not a state: a divergent round is still committed and revealed, because participation records what you actually computed.

### Inspecting state

There is no status subcommand; read the SQLite store directly. The container image has no `sqlite3` CLI but ships Python's standard-library `sqlite3`:

```bash
docker compose exec sidecar python -c "
import sqlite3
db = sqlite3.connect('/data/sidecar.db'); db.row_factory = sqlite3.Row
for r in db.execute('SELECT round_number, sidecar_state, commit_tx_hash, reveal_tx_hash, error_category, reveal_error_category FROM sidecar_rounds ORDER BY round_number DESC LIMIT 10'):
    print(dict(r))
"
```

What to expect at each checkpoint: **before commit** the round reaches `SCORED` (with an `OUTPUT_DIVERGENCE` annotation if your reproduction differs — it is still committed); **after commit** the state is `COMMITTED` with `commit_tx_hash` set; **after reveal** the state is `REVEALED` with `reveal_tx_hash` set; **after convergence** the foundation publishes a per-round report (see below).

### Restart and crash safety

Each pass is idempotent and restart-safe — you can stop, update, or crash the container without corrupting participation:

- Reveals run first and are driven from local state, independent of whether a new round is scorable, so a round committed earlier still reveals in its window even when nothing new is eligible.
- Before submitting a commit or reveal, the sidecar scans the publisher account's recent validated history for a payload you already authored for that round; if it finds one it records that transaction hash instead of double-submitting. A run that crashed after broadcasting but before persisting recovers correctly.
- The chain watcher advances its cursor past an announcement only once it is terminally handled, never past one whose commit is still pending or hit a transient error, so nothing is silently skipped.
- A reveal refuses to broadcast unless your stored outputs and salt still reproduce the committed commitment (a local-state corruption guard).

### Recoverable vs terminal conditions

Recoverable — the next pass retries automatically:

- **Insufficient relay-wallet balance at reveal time**: the reveal is skipped for this pass and retried on a later pass while the reveal window is still open (the round stays `COMMITTED`). Fund the wallet and the next pass reveals.
- **Transient RPC, download, or scoring errors**: the pass fails (`participate failed` in the logs), the cursor does not advance, and the round is reprocessed next pass.
- **A commit window that has not opened yet**: the pass holds and retries.

Terminal — there is no re-submission on a later pass:

- **Insufficient relay-wallet balance at commit time**: the commit is recorded `SKIPPED` and the announcement cursor advances past it, so the round is not committed and is not retried later. Keep the wallet funded ahead of the commit window (see *Avoiding misses*). A low-balance *reveal*, in contrast, is retried as above.
- **A reveal window that closed before you revealed** is recorded as a missed reveal: the round stays `COMMITTED` with `reveal_error_category` = `REVEAL_WINDOW_MISSED`. This is distinct from a transient low-balance reveal skip, which sets no `reveal_error_category` and is retried.
- **A commit window that closed before you committed**: the round stays `SCORED`, is never committed, and the announcement cursor advances past it.

A missed window is a chain-participation miss only — it does not fail the round's score and never affects the canonical Validator List.

### Avoiding misses

- **Keep the relay wallet funded** — maintain the account reserve plus a long runway of per-round transaction fees so a commit or reveal is never skipped for balance. (An explicit startup balance pre-flight check is not yet built; underfunding is handled reactively.)
- **Keep the poll interval well below the window lengths** — the default 60s sits comfortably inside devnet windows, so each window is polled many times.
- **Keep the container running** — the reveal happens passes after the commit, so a host that is down across the reveal window misses it even though the commit landed.

### Reading the convergence outcome

After your reveal, the foundation ingests on-chain commits and reveals, verifies them against its own outputs, and publishes a per-round convergence report. The report seals once the latest validated ledger has closed past `reveal_closes_at` plus a grace period; before that it is live and still changing. Read it from the foundation scoring service, keyed on the on-chain round number (devnet base shown):

```bash
curl https://scoring-devnet.postfiat.org/api/scoring/rounds/<round_number>/convergence
# or, for the latest announced round:
curl https://scoring-devnet.postfiat.org/api/scoring/convergence/current
```

The response carries a `phase` (`live`, `sealed`, or `not_tracked`) and a `finalized` flag. A `sealed` response includes the immutable `report` with its `convergence_bundle_cid`, the on-chain `anchor_tx_hash`, and `sealed_at`. Find your validator in the report's `participants` array by your validator master key (`nH...`); your `outcome` is one of:

- `valid` — your revealed hashes matched the foundation (or its hashes were not available to compare).
- `divergent` — your reveal was accepted but one or more output levels (`RAW`, `PARSED`, `SELECTED_UNL`) differ; `comparison_levels_matched` and `divergence_stage` show where.
- `missing_reveal` — your commit was accepted but no valid reveal was (for example a missed reveal window).
- `late` / `commitment_mismatch` / `signature_invalid` — your commit fell outside the window, a reveal did not match your accepted commitment, or no commit carried a valid signature.

These are the foundation's per-validator outcome labels and are distinct from the sidecar's local round states above. Each sealed report is also anchored on chain by a `pf_dynamic_unl_convergence_report_v1` memo from the foundation publisher account, carrying the `round_number` and `convergence_bundle_cid`, so you can resolve a round to its report by scanning that account.

## Configuration

The setup above does not require editing any environment values. For the full list of variables, including advanced tunables you should normally leave at their defaults, see [`Configuration.md`](Configuration.md).
