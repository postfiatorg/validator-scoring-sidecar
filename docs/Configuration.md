# Configuration

The sidecar reads its configuration from environment variables. In the Docker workflow, these come from `.env` (created by copying `.env.testnet.example` or `.env.devnet.example`). The example files contain working defaults for their target network; operators do not normally edit these by hand.

## Required per-network values

These are set by the env example file matching your chosen network.

| Variable | Description |
|---|---|
| `POSTFIAT_SIDECAR_NETWORK` | Network label: `testnet` or `devnet`. |
| `POSTFIAT_SCORING_BASE_URL` | Public Post Fiat scoring service base URL for the network. |
| `POSTFIAT_SIDECAR_IPFS_GATEWAY_URL` | IPFS gateway used to fetch frozen input package files when the HTTPS fallback is not available. |

## Container-managed values

These are set by `docker-compose.yml` and do not appear in the env files.

| Variable | Container value | Notes |
|---|---|---|
| `POSTFIAT_SIDECAR_DATA_DIR` | `/data` | The container always writes state under the mounted volume, regardless of any host-style path that may appear in `.env`. |

## Tuning

These have working defaults. Override only if you have a concrete operational reason; they are not part of normal setup.

| Variable | Default | Description |
|---|---|---|
| `POSTFIAT_SIDECAR_SYNC_INTERVAL_SECONDS` | `3600` | How often the container loops back for another sync. Foundation rounds happen on a weekly cadence, so the default keeps load light against the scoring service. Has no effect outside the Docker workflow. |
| `POSTFIAT_SIDECAR_TIMEOUT_SECONDS` | `30` | HTTP request timeout for calls to the scoring service and the IPFS gateway. |

To override a tunable, edit your `.env` before running `docker compose up`.

## Participation mode (opt-in)

Verify-only sync needs none of these. To run the on-chain commit-reveal loop, deploy with the participation overlay (`docker compose -f docker-compose.yml -f docker-compose.participate.yml up -d`) and uncomment the participation block in `.env`. The loop is all-or-nothing: it fails fast and changes nothing on chain if any prerequisite is missing.

| Variable | Description |
|---|---|
| `POSTFIAT_SIDECAR_MODE` | Set to `participate` to run the commit-reveal loop instead of `sync`. |
| `POSTFIAT_SIDECAR_VALIDATOR_WALLET_SEED` | Seed of the funded operator relay `r...` wallet that pays for and sends the commit/reveal transactions. Secret: read from the runtime environment only, never accepted as a CLI flag, logged, or baked into an image layer. |
| `POSTFIAT_SIDECAR_VALIDATOR_KEYS_FILE` | Host path of your `validator-keys.json`; the overlay mounts it read-only into the container. |
| `POSTFIAT_SIDECAR_PFTL_RPC_URL` | PFTL JSON-RPC endpoint for chain reads and memo submission. Defaults to `https://rpc.<network>.postfiat.org`. |
| `POSTFIAT_SIDECAR_FOUNDATION_PUBLISHER_ADDRESS` | Optional override of the foundation publisher account; auto-discovered from the scoring service config when unset. |
| `POSTFIAT_SIDECAR_CHAIN_POLL_INTERVAL_SECONDS` | How often the participate loop runs a pass (default `60`). Keep it well below the announced commit/reveal windows. |

`POSTFIAT_SIDECAR_VALIDATOR_KEYS_PATH` — the in-container path of the mounted key file — is container-managed: the overlay pins it to `/keys/validator-keys.json`. Set it yourself only when running the CLI outside Docker.

### Key handling

The sidecar never holds your validator master-key seed in process. It reads only the `public_key` field of the mounted `validator-keys.json` — the validator identity carried in each commit/reveal payload — and signs by invoking the bundled postfiatd `validator-keys` tool, explicitly bound to that same mounted file, so the embedded identity and the signature always come from one configured key source. The key file is mounted read-only, never copied into the image, and never logged. The on-chain transactions are signed and paid for by the separate relay wallet above, so the on-chain sender is deliberately not your validator identity.
