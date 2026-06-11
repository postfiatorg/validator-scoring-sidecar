# Deploying the inference runtime

The sidecar can stand up its own inference endpoint that reproduces the runtime the foundation pinned for a scoring round — the runtime it will later use to independently re-run a round and compare its result against the foundation's. There are two paths: a managed [Modal](https://modal.com) app under your own account, or a local SGLang container on your own H100. Both read the round's `runtime/execution_manifest.json` (the digest-pinned image, GPU class, tensor-parallelism degree, deterministic launch arguments, and SGLang workspace environment) and write the same `deployment_record.json`. Pick whichever matches how you want to run.

Either command runs on your own host, separately from the unattended `sync` loop. Neither is part of the `docker compose` workflow in [`Usage.md`](Usage.md).

## What these commands do and do not do

These commands stand up the endpoint and write the deployment record; they do not score anything themselves. Scoring against the endpoint, comparison to the foundation's result, and on-chain participation are done by the `score` command and the unattended participate loop, both of which read the deployment record these commands produce — see [`Usage.md`](Usage.md).

## Option 1: Modal (managed)

Prerequisites:

- A Modal account with access to the GPU class the manifest pins (currently H100).
- The sidecar installed with the Modal extra: `python -m pip install -e ".[modal]"`.
- A one-time Modal login on this host: `modal setup`. The sidecar uses this existing login to deploy and never stores or manages your Modal credentials.

In the common case, deploy from the latest eligible round:

```bash
validator-scoring-sidecar deploy-modal --network testnet
```

With no `--round-id` or `--manifest`, the command discovers the newest round that exposes a frozen input package, verifies it, and deploys from its manifest — the foundation's current pinned runtime. Pin a specific round with `--round-id <id>`, or deploy from a local file with `--manifest <path>` for testing.

| Flag | Description |
|---|---|
| `--network` | `testnet` or `devnet`. Selects defaults and the app name. |
| `--app-name` | Override the Modal app name. Defaults to `validator-scoring-sidecar-<network>`. |
| `--round-limit` | Recent rounds to scan when no `--round-id` or `--manifest` is given. |
| `--source` | Package source for round fetches: `auto`, `https`, or `ipfs`. |
| `--json` | Emit the deployment record as JSON. |

A successful run deploys a Modal app named `validator-scoring-sidecar-<network>` (so a later deploy replaces it rather than creating a second one). The endpoint scales to zero when idle, so a deployed-but-unused app does not hold a GPU; the first request after idle incurs a cold start while the container and weights load. Deploying requires no PostFiat-specific credentials beyond your Modal login.

## Option 2: Local SGLang (self-hosted)

Run the model on your own hardware instead of renting cloud GPUs.

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

## The deployment record

`deployment_record.json` is the local description of the runtime you stood up: its mode (`modal` or `local`), image, GPU class, tensor parallelism, launch arguments, environment, served model name, model revision, and endpoint URL.

The sidecar reads this record back when deciding whether your deployed runtime still matches a round's manifest. When the foundation changes its pinned runtime in a future round — a new image or model revision — the record no longer matches and the sidecar reports the round as incompatible, which is the signal to redeploy with the same command against the newer round. You do not need to redeploy per round; redeploy only when the foundation's pinned runtime changes.
