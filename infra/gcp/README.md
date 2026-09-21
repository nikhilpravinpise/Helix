# GCP target - stub

**Status: not built, not applied, not verified.** This directory exists so
the cloud requirement is scoped and visible rather than remembered. Nothing
here has ever run against a real project, and nothing in Helix imports it.

DJS23OLOE501 requires a cloud deployment (D23). Helix runs on kind today.
This records exactly what would change, so the work is a known quantity
instead of an open question.

## What the delta actually is

Smaller than it looks. The Phase 1 manifests were written with this in
mind, so the split is already clean.

| Piece | Change needed |
|---|---|
| `sandbox/payment-api/k8s/0*.yaml` | None. Plain Kubernetes objects |
| `sandbox/payment-api/k8s/06-ingress.yaml` | `ingressClassName` and host. The file says so in its own header |
| `infra/terraform/main.tf` | `kind_cluster.sandbox` becomes a `google_container_cluster`; the rest of the module is provider-agnostic |
| `infra/terraform/outputs.tf` | Three outputs reference `kind_cluster.sandbox` |
| Image loading | `scripts/load-sandbox-images.sh` does `kind load`; GKE needs a registry push (Artifact Registry) |
| `HELIX_KUBE_CONTEXT` | `kind-helix-sandbox` becomes the GKE context name |
| `HELIX_ALLOWED_KUBE_CONTEXTS` | Must list the GKE context, or the write client refuses every mutation by design (1.8) |

The observer plane does not move. Postgres, Kafka and the ELK stack run
in Docker on the host under D1/D3 precisely because they must not share a
failure domain with the cluster being deliberately broken, and that
reasoning is unchanged by the cluster being remote - if anything it gets
stronger.

## Prerequisites, none of which an agent can satisfy

1. `gcloud` SDK installed (it is not, on this machine).
2. `gcloud auth login` and `gcloud auth application-default login` -
   both interactive.
3. A project with billing attached.
4. A region and zone chosen, so a single zonal cluster can be pinned.

## Two things to get right when this is built

**Pin the cluster version.** Release channel `None`, autoscaling off,
fixed node count. If GKE auto-upgrades mid-series, subphase 1.11's
"three consecutive runs inside a tolerance band" measures GKE rather than
Helix. That is not hypothetical - two attempts at that criterion have
already been discarded for unrelated environmental reasons, and a moving
cluster version would be a third.

**Do not move the benchmark.** The benchmark rebuilds cluster state on
every run: free and instant on kind, minutes and real billing on GKE,
repeated across every scenario and every arm. The cheap shape that
satisfies the requirement is a one-off GKE deployment of `payment-api` as
a separate artifact for the report, with measured runs staying local. The
manifests are already isolated for exactly that.

## Why there is no Terraform here yet

Writing an unapplied `google_container_cluster` resource would look like
progress and prove nothing - untested infrastructure code is a liability,
not a head start, because the first real `apply` finds every mistake in it
anyway. `scripts/gcp-up.sh` refuses to run and prints the prerequisites,
which is the honest state of this target.
