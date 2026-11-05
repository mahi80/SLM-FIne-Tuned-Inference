# Architecture sign-off pack

This is the review artifact, not a brochure. It states what was built, what was measured,
what was modelled, what was not demonstrated, and the conditions under which the design may
be taken forward.

**Verdict:** the architecture is **accepted as a reference platform** with conditions. It is
**not** a production sign-off for a regulated tenant until the conditions in
[acceptance.md](acceptance.md#conditions) are closed. Those conditions are linked to risk
IDs, not to optimism. The newest evidence (A-30) is a dry-run plan for EKS, AKS and GKE, not
a provisioned cluster: no cloud account was used, and that limit is stated in N-14.

## Read in this order

| Document | Question it answers |
|---|---|
| [requirements.md](requirements.md) | Which job requirements does this platform evidence, and how honestly? |
| [acceptance.md](acceptance.md) | What is the sign-off checklist, what was verified here, and what was not? |
| [release-process.md](release-process.md) | How does a model change reach production, and how is it reversed? |
| [threat-model.md](threat-model.md) | What are the trust boundaries, assets and threats? |
| [risks.md](risks.md) | What can still go wrong, how likely, and what is left after mitigation? |
| [operations.md](operations.md) | What is the SLO, what pages someone, and what does the runbook say? |
| [evidence.json](evidence.json) | Machine-readable snapshot: commits, hashes, test counts, signature status |

Architecture views are in [../architecture/c4.md](../architecture/c4.md). Decisions are in
[../adr/](../adr/).

## Reproduce the evidence

Every claim in this pack is reproducible from a clean checkout. The commands below are the
ones used to generate [evidence.json](evidence.json); they need Docker, Python 3.10+,
`kubectl` and `openssl`.

```bash
make up                 # GPU tier, simulated tiers, gateway, observability
make smoke-gateway      # routing, auth, streaming, all three tiers
make smoke-policy       # admission, canary, audit chain, breaker
make adapter-eval       # base vs adapter v1/v2 on the held-out set
make bench              # tier benchmark matrix        -> bench/results/
make eval-gate          # golden-set release gate      -> bench/results/eval-gate.json
make cost               # ownership cost model         -> bench/results/cost-model.json
make test               # 362 unit tests, no cluster required
make deploy-validate    # rendered overlays             -> deploy/rendered/
make release-check      # signed manifest == deployment tree
make signoff            # runs the above checks and rewrites evidence.json
```

`make up`, `adapter-eval` and `bench` need the RTX 4080. Everything else runs without a
GPU, and `make test` / `make release-check` run without a cluster.

## Evidence rules used throughout

1. **Measured and MODELLED never share a cell.** A number from `src/tiersim` is labelled
   `MODELLED` and its responses carry `x-simulated: true`. If a claim depends on a modelled
   tier, the label appears in the same sentence.
2. **A gate result is an artifact, not a recollection.** `bench/results/eval-gate.json` is
   committed; the release manifest pins its SHA-256. Re-running `make eval-gate` after a
   model change produces a different file, which invalidates the manifest until it is
   rebuilt and re-signed.
3. **Static validation is not deployment validation.** Manifests render and pass policy
   checks; a kind cluster then applied the vanilla overlay and exercised Kyverno and Argo,
   which is recorded as observed evidence (A-17 to A-21). The image supply chain is
   separately evidenced: CI publishes the first-party images and every overlay renders by
   digest (A-22). No OpenShift cluster was
   available, so every OpenShift claim is marked *static* and the gap is risk
   [R-09](risks.md#r-09--no-live-openshift-validation).
4. **The signer is a demo key in this repository's threat model.** The private key is
   gitignored and was generated locally. Signature verification proves the manifest was
   signed by that key; it does not prove who holds it. See
   [release-process.md](release-process.md#key-custody).

## Document control

| Item | Value |
|---|---|
| Platform release | see the newest manifest under [`release/`](../../release) |
| Scope | 1–8B open-weight models, one fine-tuned task, three compute tiers |
| Out of scope | frontier-model training, multi-tenant GPU sharing, real cloud deployment, UI |
| Review state | self-assessed reference architecture; no external assessor |
| Last review | 2026-11-05 — pack cross-checked against `b1bd4a0` (CI run `41955890300`); record in [acceptance.md](acceptance.md#sign-off-record) |
| Change rule | a release, policy or threat-model change re-runs `make signoff` and updates this pack |
