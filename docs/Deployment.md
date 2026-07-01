# Deploying the inference runtime

The sidecar can stand up its own inference endpoint that reproduces the runtime the foundation pinned for a scoring round — the runtime it will later use to independently re-run a round and compare its result against the foundation's. There are two paths: a managed [Modal](https://modal.com) app under your own account, or a local SGLang container on your own H100. Both read the round's `runtime/execution_manifest.json` (the digest-pinned image, GPU class, tensor-parallelism degree, deterministic launch arguments, and SGLang workspace environment) and write the same `deployment_record.json`.

Deploying stands up the endpoint and writes the deployment record; it does not score anything itself. Scoring against the endpoint, comparison to the foundation's result, and on-chain participation are done by the `score` command and the unattended participate loop, both of which read the deployment record.

## Option 1: Modal (managed, zero-touch)

For Modal-backed participation there is nothing to deploy by hand. Set the four Modal values in `.env` (`MODAL_TOKEN_ID` / `MODAL_TOKEN_SECRET` account credentials and the `POSTFIAT_SIDECAR_MODAL_KEY` / `POSTFIAT_SIDECAR_MODAL_SECRET` proxy-auth pair — see [`Configuration.md`](Configuration.md)) and start the participation overlay. The participate loop deploys the manifest-pinned endpoint itself when none is recorded, and redeploys automatically when the foundation pins a new runtime in a later round, so unattended operation survives foundation runtime upgrades. On startup the participation container provisions this endpoint once, before entering the loop, so the one-time Modal image build and cold start happen ahead of the first round rather than inside its commit window; this startup warm-up is skipped for local SGLang and when Modal credentials are absent, does not redeploy an already-current endpoint, and is non-fatal — if it does not complete, the loop still provisions on demand. The only prerequisite is a Modal account with access to the GPU class the manifests pin (currently H100).

To deploy manually — for testing, or to pre-warm before a round — run the same command the loop uses, inside the participation container so the record lands in the data volume directly. The four Modal values must already be set in `.env` (see [`Configuration.md`](Configuration.md)); the command reads them from the container environment:

```bash
docker compose -f docker-compose.yml -f docker-compose.participate.yml \
  run --rm sidecar deploy-modal
```

With no `--round-id` or `--manifest`, the command discovers the newest round that exposes a frozen input package, verifies it, and deploys from its manifest — the foundation's current pinned runtime. Pin a specific round with `--round-id <id>`, or deploy from a local file with `--manifest <path>` for testing.

| Flag | Description |
|---|---|
| `--network` | `testnet` or `devnet`. Selects defaults and the app name. |
| `--app-name` | Override the Modal app name. Defaults to `validator-scoring-sidecar-<network>`. |
| `--round-limit` | Recent rounds to scan when no `--round-id` or `--manifest` is given. |
| `--source` | Package source for round fetches: `auto`, `https`, or `ipfs`. |
| `--json` | Emit the deployment record as JSON. |

A successful run deploys a Modal app named `validator-scoring-sidecar-<network>` (so a later deploy replaces it rather than creating a second one). The endpoint scales to zero when idle, so a deployed-but-unused app does not hold a GPU; the first request after idle incurs a cold start while the container and weights load.

If you run the sidecar for more than one validator against the same Modal account, set `POSTFIAT_SIDECAR_MODAL_APP_NAME` to a distinct value per validator so they do not manage the same app — see [Running multiple validators on one Modal account](Configuration.md#running-multiple-validators-on-one-modal-account). The participate loop reads that value when it auto-deploys, and an unset value keeps the per-network default.

## Option 2: Local SGLang (self-hosted)

Run the model on your own hardware instead of renting cloud GPUs. This path stays operator-managed: the sidecar never deploys, restarts, or replaces a local runtime — when a later round pins a different runtime, the sidecar reports the round as runtime-incompatible and you re-run the command below against the newer round.

Prerequisites:

- An H100 host. The local path is strict: it refuses to start on any other GPU class, because a mismatched GPU cannot produce a trustworthy comparison against the foundation.
- Docker, and `nvidia-smi` for GPU detection.
- The sidecar installed with the local extra: `python -m pip install -e ".[local]"`.

Start from the latest eligible round (same round-selection flags as `deploy-modal`):

```bash
validator-scoring-sidecar start-sglang --network testnet
```

`--round-id <id>` and `--manifest <path>` work the same way. The command downloads the manifest's pinned model snapshot, starts the manifest's digest-pinned SGLang container with the manifest's launch arguments, confirms the host GPU matches the pinned class, waits for the server to become healthy, and writes the `mode=local` deployment record.

| Flag | Description |
|---|---|
| `--port` | Local port to serve SGLang on. Defaults to `8000`. |
| `--network`, `--round-id`, `--manifest`, `--round-limit`, `--source`, `--json` | As for `deploy-modal`. |

Unlike Modal, a local container holds the GPU for as long as it runs. The command needs no Modal or PostFiat credentials.

The deployment record points at the endpoint as seen from the GPU host (`localhost` by default), which is not reachable from inside the sidecar container. Set `POSTFIAT_SIDECAR_LOCAL_ENDPOINT_URL` in `.env` to the container-reachable address — `http://host.docker.internal:8000/v1` for a runtime on the same host (the participation overlay maps that name to the host), or the GPU host's address when it is a separate machine. If the record was written on a different machine than the sidecar runs on, copy it into the data volume with `docker compose cp <path> sidecar:/data/runtime/deployment_record.json`.

## The deployment record

`deployment_record.json` is the local description of the runtime you stood up: its mode (`modal` or `local`), image, GPU class, tensor parallelism, launch arguments, environment, served model name, model revision, and endpoint URL.

The sidecar reads this record back when deciding whether your deployed runtime still matches a round's manifest. When the foundation changes its pinned runtime in a future round — a new image or model revision — the record no longer matches. For a Modal record the participate loop handles this itself by redeploying from the newer manifest; for a local record the sidecar reports the round as runtime-incompatible, which is the signal to re-run `start-sglang` against the newer round. Nothing needs redeploying per round; the runtime changes only when the foundation's pinned manifest does.

## Upgrades

The runtime you score with is matched against each round's pinned `runtime/execution_manifest.json` before any inference runs (the compatibility gate). What an upgrade asks of you depends on what changed; in every case the gate keeps the sidecar from scoring against a runtime that cannot be honestly compared.

### The foundation pins a new runtime (model revision or image)

The routine case — the manifest still uses a schema and a parser/selector this sidecar supports, only the model revision or runtime image moved.

- **Modal (zero-touch):** nothing to do. The participate loop notices the deployment record no longer matches and redeploys the manifest-pinned endpoint before scoring — but only after a pre-check confirms a fresh deployment would actually match, so it never loops deploying a runtime that still would not pass. Run `deploy-modal` yourself only to pre-warm (see [Option 1](#option-1-modal-managed-zero-touch)).
- **Local SGLang (operator-managed):** the round is reported runtime-incompatible (`error_category` `MANIFEST_INCOMPATIBLE`) and nothing is scored until you re-run `start-sglang` against the newer round. The sidecar never replaces a runtime it does not own (see [Option 2](#option-2-local-sglang-self-hosted)).

### The sidecar version changes

Operators run published images. Upgrade with the standard flow ([Updating the sidecar](Usage.md#updating-the-sidecar)):

```bash
docker compose pull
docker compose up -d
```

Add the participation overlay to both commands if you run participation. State in the named volume is untouched.

### The foundation moves past the vendored parser or selector

The sidecar reproduces the foundation's parsing and UNL selection with foundation code vendored at pinned content hashes. If the foundation deploys a behavioral change to its parser or selector, the round's manifest carries a parser/selector `content_sha256` outside the set this sidecar build recognizes, and the round is recorded `MANIFEST_INCOMPATIBLE` ("vendor refresh required"). An operator does not fix this locally — it needs a newer sidecar image whose vendored copy matches the foundation's new code. Until such an image is published and pulled, the round is correctly left unverified rather than compared against stale logic. (Refreshing the vendored copy is a maintainer task — see the repository `README.md`.)

### When the sidecar declines a round instead of scoring it

Because the compatibility gate runs before inference, the sidecar declines a round rather than force a misleading comparison. "Declines" here means the round is recorded as a scoring failure (`sidecar_state` `SCORING_FAILED`) carrying the `error_category` below — only override and dry-run rounds use the dedicated `SKIPPED` state.

| Condition | Signal in `sidecar_rounds` | What to do |
|---|---|---|
| Manifest `schema_version` is newer than this sidecar supports | `error_category` `MANIFEST_UNSUPPORTED` | Upgrade the sidecar image. |
| Manifest parser/selector `content_sha256` is outside the supported set | `error_category` `MANIFEST_INCOMPATIBLE` ("vendor refresh required") | Upgrade the sidecar image once one carrying the new vendor is published. |
| Deployed runtime does not match the manifest (model revision, image digest, launch args, GPU, tensor parallelism) | `error_category` `MANIFEST_INCOMPATIBLE` | Modal redeploys automatically in the participate loop; local SGLang: re-run `start-sglang`. |
| Override round | `sidecar_state` `SKIPPED` (`error_category` `SKIPPED_OVERRIDE`) | Nothing — intentionally never scored. |
| Dry-run round | `sidecar_state` `SKIPPED` (`error_category` `SKIPPED_OPERATOR_OPT_OUT`) | Nothing — intentionally never scored. |

On Modal the automatic redeploy is suppressed for the cases a fresh deployment could not fix — an unsupported schema, a vendored-code mismatch, or an override/dry-run round — so the loop never burns a deploy on a manifest it cannot satisfy; only a runtime mismatch (third row) triggers an automatic redeploy.

For how these states surface during a round, see [Troubleshooting](Usage.md#troubleshooting) and [Participation lifecycle and recovery](Usage.md#participation-lifecycle-and-recovery) in the usage guide.
