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
