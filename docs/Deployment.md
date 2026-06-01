# Deploying the Modal inference runtime

The sidecar can stand up its own inference endpoint on [Modal](https://modal.com), under your own account, that reproduces the runtime the foundation pinned for a scoring round. This is the runtime the sidecar will use to independently re-run a round and compare its result against the foundation's.

The `deploy-modal` command reads a round's `runtime/execution_manifest.json` — the digest-pinned container image, GPU class, tensor-parallelism degree, deterministic launch arguments, and SGLang workspace environment — and deploys a matching endpoint from those exact values. It then writes a local `deployment_record.json` describing what it deployed.

This runs on your own host, separately from the unattended `sync` loop. It is not part of the `docker compose` workflow in [`Usage.md`](Usage.md).

## What it does not do yet

This command deploys the endpoint and records it. It does not yet run scoring against that endpoint or compare outputs to the foundation; that lands in a later release. Today its value is standing up the runtime and producing the deployment record that the sidecar's compatibility check reads.

## Prerequisites

- A Modal account with access to the GPU class the manifest pins (currently H100).
- The sidecar installed with the Modal extra:

  ```bash
  python -m pip install -e ".[modal]"
  ```

- A one-time Modal login on this host:

  ```bash
  modal setup
  ```

  The sidecar uses this existing login to deploy. It never stores or manages your Modal credentials.

## Deploying

In the common case, deploy from the latest eligible round:

```bash
validator-scoring-sidecar deploy-modal --network testnet
```

With no `--round-id` or `--manifest`, the command discovers the newest round that exposes a frozen input package, verifies it, and deploys from its manifest. That manifest reflects the foundation's current pinned runtime, which is what you usually want to match. Rounds without a frozen input package (override, dry-run, or legacy) are skipped.

To pin to a specific round by its ID:

```bash
validator-scoring-sidecar deploy-modal --round-id <id> --network testnet
```

The command fetches and verifies that round's frozen input package (the same verification `sync` and `fetch-input-package` perform), reads the manifest from the verified package, and deploys from it.

You can also deploy directly from a manifest file, for testing or debugging:

```bash
validator-scoring-sidecar deploy-modal --manifest path/to/execution_manifest.json
```

Useful flags:

| Flag | Description |
|---|---|
| `--network` | `testnet` or `devnet`. Selects defaults and the app name. |
| `--app-name` | Override the Modal app name. Defaults to `validator-scoring-sidecar-<network>`. |
| `--round-limit` | Recent rounds to scan when no `--round-id` or `--manifest` is given. |
| `--source` | Package source for round fetches: `auto`, `https`, or `ipfs`. |
| `--json` | Emit the deployment record as JSON. |

A successful run deploys a Modal app named `validator-scoring-sidecar-<network>` (so a later deploy replaces it rather than creating a second one) and writes the deployment record to `<data-dir>/runtime/deployment_record.json`.

## The deployment record

`deployment_record.json` is the local description of the runtime you deployed: its mode, image, GPU class, tensor parallelism, launch arguments, environment, served model name, model revision, and endpoint URL.

The sidecar reads this record back when deciding whether your deployed runtime still matches a round's manifest. When the foundation changes its pinned runtime in a future round — a new image or model revision — the record no longer matches and the sidecar reports the round as incompatible, which is the signal to redeploy:

```bash
validator-scoring-sidecar deploy-modal --round-id <newer-id> --network testnet
```

You do not need to redeploy per round. Redeploy only when the foundation's pinned runtime changes.

## Cost

The deployed endpoint scales to zero when idle, so a deployed-but-unused app does not hold a GPU. The first request after idle incurs a cold start while the container and model weights load.

## Authentication

Deploying uses your local `modal setup` login. The command requires no PostFiat-specific credentials.
