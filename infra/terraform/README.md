# infra/terraform - the environment as code (subphase 1.13)

One flat Terraform root that owns the whole Phase 1 environment:

| Provider | Manages |
|---|---|
| `tehcyx/kind` | the `helix-sandbox` kind cluster, generated from `infra/kind-config.yaml` (the file is the single source of truth - `scripts/up.sh` uses it too) |
| `kreuzwerker/docker` | host-side containers: Postgres, Elasticsearch, Kibana, Jenkins (built from `infra/jenkins`), SonarQube - same images/env/limits/ports as the compose files |
| `alekc/kubectl` | every committed Kubernetes manifest: ingress-nginx (pinned), the payment-api namespace/deploy/service/ingress set, `infra/helix-ingress.yaml` |
| `terraform_data` + local-exec | the image build/load step (`scripts/load-sandbox-images.sh`, shared with the script path) and `infra/filebeat-daemonset.yaml` (see the NOTE in kubernetes.tf for why that one file cannot be a `kubectl_manifest`) |

## Usage

```bash
cd infra/terraform
cp terraform.tfvars.example terraform.tfvars   # then edit as needed
terraform init
```

On a **clean machine** (no kind cluster yet), apply is two commands -
the kubectl provider needs a readable kubeconfig, which only exists once
the cluster does:

```bash
terraform apply -target=kind_cluster.sandbox   # creates the cluster
terraform apply                              # everything else
```

Once the cluster exists, plain `terraform plan` / `terraform apply` are
single-command. `terraform destroy` tears down the cluster (and with it
every in-cluster object) plus the containers it created.

## Adopting an already-running environment

The kind provider has **no import support** (verified in the provider
source - no `Importer` on the resource). A cluster created by `kind
create` cannot be adopted; the one-time transition is:

```bash
kind delete cluster --name helix-sandbox
terraform apply -target=kind_cluster.sandbox && terraform apply
```

The sandbox is stateless, so this loses nothing. Postgres is different:
remove only the *container*, keep the volume (terraform names it
`helix-state_postgres-data`, the same name compose uses, so whichever path
created it is the one the other adopts):

```bash
docker rm -f helix-postgres      # volume helix-state_postgres-data stays
terraform apply                  # recreates the container on that volume
```

The reverse direction works too - `docker compose -f
infra/postgres-compose.yml up -d` reuses the terraform-created volume.
`ignore_changes = [labels]` on the volumes is what makes that handoff
safe: without it terraform would force-replace a compose-labeled volume
and delete the data.

## Memory-tight hosts (CF-4)

`enable_logging` / `enable_jenkins` / `enable_sonarqube` gate the heavy
stacks. The minimal useful apply is the cluster + Postgres:

```hcl
# terraform.tfvars
enable_logging   = false
enable_jenkins   = false
enable_sonarqube = false
```

Never run kind + ELK + Jenkins + SonarQube simultaneously on a tight
host - flip flags per session or use the compose files for spot checks.

## Known benign drift

`terraform plan` will always show two pending creates: the ingress-nginx
`admission-create`/`admission-patch` cert Jobs carry
`ttlSecondsAfterFinished`, so the cluster reaps them minutes after they
succeed. Reapplying them is harmless (they only ensure the webhook cert
secret exists) - or leave them; the webhook keeps working.

## Platform notes

- `docker_host` defaults to the Docker Desktop named pipe
  (`npipe:////./pipe/docker_engine`); on Linux set it to
  `unix:///var/run/docker.sock` in terraform.tfvars.
- local-exec provisioners invoke `bash` - on Windows that resolves to Git
  Bash, which must be on PATH ahead of WSL's `bash.exe` (`where bash`
  should list `C:\Program Files\Git\usr\bin\bash.exe` first).
- `infra/helix-ingress.yaml` uses an ExternalName to
  `host.docker.internal`, which resolves only under Docker Desktop. On a
  Linux kind host, swap in the selectorless-Service + manual-Endpoints
  variant documented in that file's header.
- SonarQube needs `vm.max_map_count >= 524288` on the Docker host's Linux
  VM - see `infra/sonarqube-compose.yml` / `infra/ansible/`.
- State is local (`terraform.tfstate`, gitignored) - Phase 1 is a
  single-host sandbox, not shared infrastructure.
