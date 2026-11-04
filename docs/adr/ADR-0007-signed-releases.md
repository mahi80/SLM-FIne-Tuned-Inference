# ADR-0007 — Sign releases with Ed25519 over canonical JSON and bind them to the deployment

- **Status:** Accepted
- **Date:** 2026-11-04
- **Deciders:** Platform architecture
- **Related:** ADR-0004 (policy router), ADR-0005 (release gate), ADR-0006 (GitOps)

## Context

By Phase 5 the platform could deploy a bundle, but "the bundle" was scattered across the
repository: the model revision lived in `compose.yaml` and the Kustomize command, adapter
weights lived in a gitignored directory, the gate result lived in `bench/results/`, and image
tags lived in a Kustomize file. Nothing tied them together, so a reviewer could not answer
one question without reading five files and trusting that they agreed.

Three constraints shaped the answer:

1. **The evidence must survive a laptop.** Adapters are 127 MB each, images are pulled from
   public registries, and the original evaluation ran on one GPU. A release artifact has to
   be small, reviewable in a pull request and verifiable offline.
2. **The target includes an air-gapped site.** Any signing scheme that requires reachability
   to a transparency log or a hosted CA fails at the site that most needs provenance.
3. **The claim must be falsifiable.** "Signed" means nothing unless a check fails when the
   policy, golden set, adapter or image changes after signing. Verification had to be wired
   into the deployment path, not run once by hand.

## Options considered

### Signature scheme

| Option | Where it wins | Where it loses |
|---|---|---|
| **Ed25519 detached signature via `openssl`** | No new dependency; deterministic signatures; works offline; key never imported into Python | Key custody is manual; no identity or transparency log |
| Sigstore / cosign | Keyless identity, transparency log, ecosystem tooling | Needs network access to Fulcio/Rekor; adds a CA trust decision; awkward air-gapped |
| GPG detached signature | Widely understood, offline | Key management and agent UX are worse than a single Ed25519 key; signatures are not deterministic |
| HMAC-SHA256 | Trivial implementation, stdlib only | Symmetric: every verifier can sign; useless as provenance between roles |
| Unsigned manifest with hashes | Simple | Proves nothing about who authorised the bundle |

### Signed payload

| Option | Where it wins | Where it loses |
|---|---|---|
| **Canonical JSON** (sorted keys, no insignificant whitespace) | Reviewable diff; reformatting does not invalidate the signature; hashing is explicit | Needs a canonical form implemented in one place and tested |
| Raw file bytes | Simplest to verify | Any editor or formatter breaks the signature and invites re-signing without review |
| Hash of a tarball of artifacts | Covers bytes directly | 250+ MB per release, no diffs, and the tarball cannot be reviewed in a PR |

### Binding to deployment

| Option | Where it wins | Where it loses |
|---|---|---|
| **Re-render overlays and compare against the manifest** | Catches drift in images, revision, policy, adapters and gate; runs without a cluster | Proves the *tree*, not the running pods |
| Admission policy verifies signature in-cluster | Proves what the cluster runs | Kyverno cannot call openssl without a custom image; needs key distribution in-cluster |
| Trust the GitOps diff | Nothing new to build | Argo shows what changed, not whether it was signed or gated |
| Cosign verify at admission | Standard for images | Signature covers images, not the model revision, policy and adapter set |

## Decision

1. **A release manifest is the unit of provenance.** `config/release.yaml` declares release
   id, model, images, adapters, policy and the evidence paths. `python -m registry build`
   turns it plus the artifacts into `release/<id>.json`, which records hashes of the adapter
   tree, policy, golden set, gate report and baseline, and the measured benchmark/cost
   figures. It refuses to build when the gate failed, an artifact is missing, or the golden
   set no longer matches the report that gated it. Text artifacts are hashed with CRLF
   normalised to LF, so the same file carries the same digest on a Windows working tree and
   on a Linux runner; adapter weights are hashed raw.
2. **The signature covers canonical JSON.** `openssl pkeyutl -sign -rawin` with an Ed25519
   key produces `release/<id>.json.sig`. Verification re-canonicalises the file, so
   formatting changes are free and value changes are fatal. Signatures are deterministic and
   tested to be byte-identical across runs.
3. **The public key is committed; the private key never is.** `release/keys/*.pem` is
   gitignored except `*.pub.pem`. Verification needs only the repository and `openssl`, which
   makes the air-gap case ordinary rather than special.
4. **The deployment must match the signed manifest.** `tools/validate_release.py` checks the
   signature, re-hashes the evidence, re-derives `config/release.yaml`, renders all three
   overlays and asserts that images, model revision, served name, adapter set, policy
   digest and in-cluster gate target agree with the manifest. It also checks that
   `compose.yaml` runs the same revision and runtime tag as the cluster release.
5. **A failed gate cannot be signed around.** There is no `--allow-failing-gate`. The gate is
   re-read from the artifact at build time, not trusted from a log line.
6. **Evidence is a committed snapshot.** `make signoff` runs the tests, verifies the release
   and writes `docs/signoff/evidence.json` with artifact hashes, test counts and signature
   status. Two releases can be diffed without rerunning anything.

## Consequences

**Measured and tested**

- `python -m registry verify` on the committed release: signature verified with the
  committed public key.
- Tamper tests: changing one value in the manifest, removing the signature, or changing an
  adapter digest each fail verification. Reformatting the JSON does not.
- `python -m tools.validate_release`: `OK release slm-0.5.0 = signed manifest = 3 overlays`.
- Drift tests: an image tag, model revision, served name, adapter name, policy digest, gate
  model or compose revision that differs from the manifest is rejected.
- 38 registry/release tests and 106 total unit tests pass without a cluster or GPU.

**Costs accepted**

- The manifest pins **tags, not digests** (risk R-02). Moving a registry tag can invalidate
  the binding without breaking a signature. Closing this is condition C-02.
- There is **no transparency log and no identity**. The committed key proves consistency, not
  who signed. Key custody is a documented procedure (R-04), not an enforced one.
- Rebuilding a release requires **retraining the adapter** — training is not bit-reproducible
  (R-13). The manifest detects divergence; it cannot prevent it.
- The signature covers **artifacts, not the running pods**. A cluster that drifts after sync
  is caught by Argo and by the in-cluster gate, not by the manifest check.

**Rejected alternatives**

- **Cosign/Sigstore** would be the right answer for a connected, multi-team estate with a
  real OIDC identity. It is the wrong answer for the air-gapped reference target, and adding
  it later is additive: the manifest digest is already the thing to sign.
- **Signing a tarball** was rejected because a release must be reviewable. A 250 MB binary
  blob in a PR is the opposite of evidence.
- **In-cluster signature verification at admission** was rejected for this phase because it
  requires key distribution into the cluster and a custom admission image. It is a natural
  extension once images are pinned by digest (C-02).
