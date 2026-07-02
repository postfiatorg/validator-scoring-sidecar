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
| `POSTFIAT_SIDECAR_INFERENCE_TIMEOUT_SECONDS` | `180` | Upper bound on one inference request's read timeout during participation scoring. It is only an upper bound: when a round's commit window is close the timeout is shortened to the time remaining, and scoring is skipped entirely if too little is left. Raise it only for a slower self-hosted SGLang runtime that legitimately needs longer than 180s per round; the zero-touch Modal path does not. |
| `POSTFIAT_SIDECAR_COMMAND_TIMEOUT_SECONDS` | sync `900`, participate derived | Per-pass watchdog for the Docker loop: if a pass runs longer than this the container is terminated and the restart policy recovers it. Sync mode uses a fixed `900`. Participate mode derives its default so the watchdog always contains a full inference (`inference timeout + 180`, with a `360` floor); if you set it explicitly for participate it must exceed the inference timeout plus 60s of head-room or the container fails fast at startup, so that the watchdog can never fire in the middle of a legitimate inference. Has no effect outside the Docker workflow. |

To override a tunable, edit your `.env` before running `docker compose up`.

The inference timeout and the watchdog are coupled: the watchdog budget must be
large enough to contain a full inference plus the fetch/verify/submit work
around it. Keep that in mind if you raise `POSTFIAT_SIDECAR_INFERENCE_TIMEOUT_SECONDS`
for a slow runtime — either leave `POSTFIAT_SIDECAR_COMMAND_TIMEOUT_SECONDS`
unset so it is derived, or set it comfortably above the inference timeout.

## Participation mode (opt-in)

Verify-only sync needs none of these. To run the on-chain commit-reveal loop, deploy with the participation overlay (`docker compose -f docker-compose.yml -f docker-compose.participate.yml up -d`) and uncomment the participation block in `.env`. The loop is all-or-nothing: it fails fast and changes nothing on chain if any prerequisite is missing.

| Variable | Description |
|---|---|
| `POSTFIAT_SIDECAR_MODE` | Set to `participate` to run the commit-reveal loop instead of `sync`. |
| `POSTFIAT_SIDECAR_VALIDATOR_WALLET_SEED` | Secret for the funded operator relay `r...` wallet that pays for and sends the commit/reveal transactions — either an XRPL `s...` family seed or the 24-word BIP39 recovery phrase Task Node issues (paste it as-is). Secret: read from the runtime environment only, never accepted as a CLI flag, logged, or baked into an image layer. |
| `POSTFIAT_SIDECAR_VALIDATOR_KEYS_FILE` | Host path of your `validator-keys.json`; the overlay mounts it read-only into the container. |
| `POSTFIAT_SIDECAR_PFTL_RPC_URL` | PFTL JSON-RPC endpoint for chain reads and memo submission. Defaults to `https://rpc.<network>.postfiat.org`. |
| `POSTFIAT_SIDECAR_FOUNDATION_PUBLISHER_ADDRESS` | Optional override of the foundation publisher account; auto-discovered from the scoring service config when unset. |
| `POSTFIAT_SIDECAR_CHAIN_POLL_INTERVAL_SECONDS` | How often the participate loop runs a pass (default `60`). Keep it well below the announced commit/reveal windows. |

`POSTFIAT_SIDECAR_VALIDATOR_KEYS_PATH` — the in-container path of the mounted key file — is container-managed: the overlay pins it to `/keys/validator-keys.json`. Set it yourself only when running the CLI outside Docker.

### Inference runtime

Participation scoring needs an inference runtime; configure exactly one of the two:

| Variable | Description |
|---|---|
| `MODAL_TOKEN_ID`, `MODAL_TOKEN_SECRET` | Modal account credentials (secret). When set, the participate loop owns the runtime: it deploys the manifest-pinned Modal endpoint when none is recorded and redeploys when the foundation pins a new runtime, with no operator action. When unset, the loop never attempts a deployment. |
| `POSTFIAT_SIDECAR_MODAL_KEY`, `POSTFIAT_SIDECAR_MODAL_SECRET` | Proxy-auth credentials for calling the deployed Modal endpoint (secret). |
| `POSTFIAT_SIDECAR_LOCAL_ENDPOINT_URL` | For self-hosted local SGLang runtimes only: the endpoint as reachable **from inside the container**. The deployment record written by `start-sglang` points at `localhost`, which inside the container is not the host — use `http://host.docker.internal:8000/v1` for a runtime on the same host (the participation overlay maps that name to the host). |
| `POSTFIAT_SIDECAR_MODAL_APP_NAME` | Optional Modal app name override (and, derived from it, the model-weights volume name). Defaults to `validator-scoring-sidecar-<network>`. Leave unset for a single validator; set a unique value per validator when several share one Modal account — see below. |

Auto-provisioning is strictly a Modal behavior. A local-mode deployment record always takes precedence and is never replaced; if a local runtime no longer matches a round's manifest, the round is recorded as runtime-incompatible and the operator re-runs `start-sglang` on their GPU host — the sidecar does not manage hardware it does not own.

### Running multiple validators on one Modal account

Each sidecar instance is independent, but two values must be unique per validator when several run against shared infrastructure:

- `POSTFIAT_SIDECAR_MODAL_APP_NAME` — set a distinct name per validator (for example `validator-scoring-sidecar-devnet-nurgle`). The app name defaults to `validator-scoring-sidecar-<network>`, so without this every instance would deploy and redeploy the *same* Modal app and compete over it when the foundation pins a new runtime. A distinct app name also gives each validator its own model-weights volume. The manual `deploy-modal --app-name` flag still overrides this when set.
- `POSTFIAT_SIDECAR_VALIDATOR_WALLET_SEED` — each validator must use its own funded relay wallet. Sharing one relay wallet across instances makes concurrent commit/reveal submissions from the same account collide on transaction sequence.

A single — or the first — validator can leave `POSTFIAT_SIDECAR_MODAL_APP_NAME` unset and keep the default.

### Key handling

The sidecar never holds your validator master-key seed in process. It reads only the `public_key` field of the mounted `validator-keys.json` — the validator identity carried in each commit/reveal payload — and signs by invoking the bundled postfiatd `validator-keys` tool, explicitly bound to that same mounted file, so the embedded identity and the signature always come from one configured key source. The key file is mounted read-only, never copied into the image, and never logged. The on-chain transactions are signed and paid for by the separate relay wallet above, so the on-chain sender is deliberately not your validator identity.
