# Overview

This is a plain-language overview of what the validator-scoring-sidecar does and
how to run it reliably. It is written for a validator operator. For the exact
commands and environment variables see [`Usage.md`](Usage.md),
[`Configuration.md`](Configuration.md), and [`Deployment.md`](Deployment.md).

## What it is for

The Post Fiat foundation runs a scoring service that decides the **Dynamic UNL** —
the Unique Node List, the set of validators your node trusts to agree on the
ledger. Each round (roughly weekly) the foundation re-scores validators and
updates that list.

The sidecar runs on your validator host and lets you **check the foundation's
work instead of trusting it blindly**, and optionally **vote on it**. It
downloads exactly the inputs the foundation scored, re-runs the scoring yourself,
compares your result to theirs, and — in the full mode — records your independent
result on-chain so there is a public, validator-signed proof that you reproduced
the round.

It does **not** publish Validator Lists, change list authority, or touch
consensus. It is a verification and participation companion to your validator,
not a replacement for the foundation.

## What happens each round

```
 FOUNDATION (on PFTL)                 SIDECAR (your validator host)
 ────────────────────                 ─────────────────────────────

 freeze the round's inputs
 announce on-chain  ───────────▶  1. see the announcement
 (memo carries the input hash,        (provably foundation-authored:
  CID, and the commit/reveal             PFTL signs the sender)
  time windows)                            │
                                           ▼
                                  2. fetch + verify the frozen inputs
                                     (verified against the hash in the
                                      on-chain memo — see "Trust" below)
                                           │
                                           ▼
                                  3. score on your own inference runtime
                                     → three output fingerprints (hashes)
                                           │
 publish final bundle  · · · · · · · · ·  ┊ compare to the foundation
 (whenever scoring finishes) · · · ·▶      (now if available, else later;
                                     ┊      never blocks commit/reveal)
                                           │
                                  ┌─ commit window ─┐
                                  ▼                 │
                       4. commit: a sealed result on-chain
                          (your fingerprints + a secret salt)
                                           │
                                  ┌─ reveal window ─┐   (opens after commit closes)
                                  ▼                 │
                       5. reveal: open the sealed result
                          (the fingerprints + the salt)
```

In order, each round:

1. **The foundation announces on-chain.** It posts a round announcement from its
   known publisher account on the Post Fiat Ledger (PFTL). Because PFTL signs the
   sender of every transaction, anything from that account is provably
   foundation-authored. The announcement carries the commit and reveal **time
   windows** and a pointer to the round's inputs.
2. **The sidecar fetches and verifies the frozen input package** — the exact,
   locked set of inputs the foundation scored — and checks every file against the
   hash, so it knows it is scoring identical, untampered inputs.
3. **It scores them on your inference runtime** and produces three fingerprints:
   the model response, the validator scores, and the selected UNL. It compares
   these to the foundation's whenever the foundation's results are available.
4. **It commits (commit window).** Commit-reveal is a two-step way to vote without
   copying. The commit publishes a *sealed envelope*: your fingerprints scrambled
   with a secret random salt. Locked in and timestamped, but unreadable.
5. **It reveals (reveal window).** Later it opens the envelope — the fingerprints
   plus the salt — proving they match what you sealed before anyone could see
   your answer.

## Trust: what anchors verification

The **on-chain announcement is the trust anchor**, not IPFS. The sidecar takes the
input hash (and CID) from the foundation's signed announcement, confirms a
matching round, then fetches the package (IPFS or HTTPS) and verifies its contents
against that on-chain hash. The content is checked against the chain, not trusted
on its own.

## Who signs, who pays

This is the key safety property of the participate mode:

```
   commit / reveal memo
   ┌─────────────────────────────────────────────┐
   │ payload: output fingerprints + salt          │
   │ signed by: VALIDATOR MASTER KEY              │ ← proves WHICH validator
   │   (via the postfiatd `validator-keys` tool,  │    produced the result
   │    which holds the secret key itself)        │
   └─────────────────────────────────────────────┘
                     │ wrapped in a PFTL payment
                     ▼
   sent + paid by:  OPERATOR RELAY WALLET  (an ordinary r... address)
                     │                       ← pays the fee; NOT your identity
                     ▼
   to:              FOUNDATION PUBLISHER ACCOUNT
                                             ← so the foundation can read it
```

The sidecar **never holds your validator master key**. Authorship is signed by the
postfiatd `validator-keys` tool, which manages the secret key itself; the sidecar
only reads the *public* master key so it knows which identity it is committing as.
The transaction is paid for and broadcast by a **separate funded relay wallet** —
so the account that sends the transaction is deliberately *not* your validator
identity.

## The lifecycle, as local state

The sidecar tracks each round in a local database, advancing it through:

```
DISCOVERED → INPUT_PACKAGE_VERIFIED → SCORED → COMMITTED → REVEALED
```

`SCORING_FAILED` / `SKIPPED` are off-ladder outcomes (could not score, or
intentionally not participating). Whether your result matched the foundation is a
*separate* note on the round, not a lifecycle stage — it can fill in after the
round is already committed or revealed.

## The two run modes

| Mode | What it does | Needs |
|------|--------------|-------|
| **`sync`** (default) | Verify-only: discover the latest round, download and verify the frozen inputs, cache them. Nothing on-chain. | Nothing extra — no wallet, keys, or GPU. |
| **`participate`** | The full lifecycle: score, commit, and reveal on-chain in the announced windows. | Inference runtime **and** the participation prerequisites below. |

`sync` is the safe default and a continuous integrity monitor for the foundation's
frozen inputs. It needs no validator at all, which makes it a zero-risk first
step for anyone. Actually checking the foundation's *scoring* — and voting —
needs `participate`.

## Participation is all-or-nothing

Set `POSTFIAT_SIDECAR_MODE=participate`. It **refuses to start** — changing nothing
on-chain — unless all of these are present:

- a **funded operator relay wallet seed or 24-word recovery phrase** (`POSTFIAT_SIDECAR_VALIDATOR_WALLET_SEED`);
- **validator-keys access** (`POSTFIAT_SIDECAR_VALIDATOR_KEYS_PATH`, read-only);
- a **reachable PFTL RPC** (probed at startup);
- a **discoverable foundation publisher address** (set it, or read from the
  foundation config endpoint).

You also need a GPU **inference runtime** that matches the foundation's pinned
setup — managed Modal (H100) or local SGLang (your own H100) — to actually score.
Running it costs money, which is why participation is "go full or don't bother":
if you are not prepared to run inference and fund a wallet, stay on `sync`.

## Running it reliably

The sidecar is built to run unattended and survive restarts: all progress lives in
a local SQLite database on the mounted Docker volume, so after a restart it resumes
where it left off, reveals anything it already committed, and **will not
double-submit** (it checks both its own state and the chain before sending). It
only advances past an announcement once that round is fully handled, so nothing is
silently skipped.

To keep it working continuously:

- **Keep it running** — `docker compose up -d`; the container loops and
  auto-restarts after reboots or crashes.
- **Keep the relay wallet funded.** Each round costs two small PFTL transaction
  fees (commit + reveal). The amounts are tiny, so a modest balance lasts a very
  long time — fund the relay wallet comfortably above the account reserve plus
  many rounds of fees, and set a low-balance alert. If it runs dry, the sidecar
  records a low-balance skip rather than crashing, but you miss that round's vote.
- **Keep validator-keys mounted** and the `validator-keys` tool reachable inside
  the container, or commit/reveal cannot be signed.
- **Keep the PFTL RPC reachable**, ideally one with enough transaction history so
  the sidecar can resume from its cursor without gaps.
- **Keep the poll interval short** (the 60-second default is fine) so you reliably
  catch the commit and reveal windows.
- **The foundation service must be live and announcing.** The sidecar follows the
  foundation; when no round is announced there is simply nothing to do that pass,
  which is normal.

## Checklist

- Start verify-only: copy `.env.testnet.example` (or `.env.devnet.example`) to
  `.env`, then `docker compose up -d`.
- Confirm a healthy first sync in the logs (`sync completed`).
- Before participating, stand up an inference runtime (Modal or local H100) and
  accept the GPU cost.
- For participate: set `POSTFIAT_SIDECAR_MODE=participate` and supply the relay
  wallet seed, validator-keys path, a reachable RPC, and the publisher address.
  Expect it to fail fast if any is missing.
- Fund the relay wallet generously and set a low-balance alert.
- Keep the container running and the poll interval short.
- Let the local SQLite state persist on the volume — do not wipe it
  (`docker compose down -v`) unless you intend a clean slate.
- Redeploy the inference runtime only when the foundation changes its pinned
  model; the sidecar flags incompatible rounds.
