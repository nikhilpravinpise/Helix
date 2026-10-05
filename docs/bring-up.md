# Bring-up - environment from scratch (subphase 1.13, Phase 2 in §6.5)

Everything needed to rebuild the whole environment on a clean machine,
using only committed code and this document. Written for Windows +
Docker Desktop (the dev host this was verified on); Linux deltas are
called out where they differ.

## 0. What you end up with

- a kind cluster `helix-sandbox` running `payment-api` (the deliberately
  breakable sandbox service) behind ingress-nginx
- host-side containers: Postgres (Helix state), plus optional
  Elasticsearch/Kibana (log store), Jenkins (CI), SonarQube (quality gate)
- two ingress hostnames: `http://payment-api.local` (the sandbox app) and
  `http://helix.local` (the Helix API/UI running on the host)

## 1. Prereqs

| Tool | Check | Install (Windows) |
|---|---|---|
| Docker Desktop | `docker info` | https://docs.docker.com/desktop/setup/install/windows-install/ |
| Git Bash | comes with Git for Windows | https://git-scm.com/download/win |
| kind | `kind --version` (v0.33+) | `winget install Kubernetes.kind` |
| kubectl | `kubectl version --client` | `winget install Kubernetes.kubectl` |
| uv | `uv --version` | `winget install astral-sh.uv` |
| python3 | `python3 --version` (3.12+) | `winget install Python.Python.3.12` - the fault scripts parse kubectl JSON with `python3`; on Windows also copy `python.exe` to `python3.exe` in the install dir (the installer ships no `python3` alias) |
| Terraform | `terraform version` (v1.5+) | `winget install Hashicorp.Terraform` |
| Ansible | optional - see `infra/ansible/` | runs inside WSL2, not natively on Windows |

Start Docker Desktop and wait for `docker info` to answer. Everything
below assumes Git Bash (`bash`).

The Ansible playbook (`infra/ansible/playbook.yml`) performs the same
checks on a Linux host/WSL2 and installs the single-binary tools there -
see `infra/ansible/README.md` for why Windows provisioning is
winget-by-hand instead.

## 2. Hostnames

Two names must resolve to the host loopback. Either edit
`C:\Windows\System32\drivers\etc\hosts` (run the editor as Administrator)
and add:

```text
127.0.0.1 payment-api.local
127.0.0.1 helix.local
```

or skip the hosts file entirely and pass `curl --resolve` in the
verification commands below (shown in both).

## 3. Bring up the environment

Two equivalent paths - **Terraform is the documented one**; the scripts
are the escape hatch (and what Terraform's steps wrap internally).

### Path A - Terraform (documented path)

```bash
cd infra/terraform
cp terraform.tfvars.example terraform.tfvars   # edit as needed
terraform init
terraform apply -target=kind_cluster.sandbox   # clean machines only - see note
terraform apply
```

Why the two-step on a clean machine: the kubectl provider needs a
kubeconfig that only exists after the cluster does. Once the cluster
exists, `terraform plan`/`apply` are single-command. Details, adoption of
a pre-existing cluster (`kind delete` + recreate - the provider has no
import support), and the enable_* memory flags: `infra/terraform/README.md`.

### Path B - scripts (escape hatch)

```bash
bash scripts/db-up.sh        # Postgres (or leave it to Path A / compose)
bash scripts/up.sh           # cluster + images + ingress-nginx + manifests
bash scripts/kafka-up.sh     # Kafka + its topics (subphase E2 onward only)
```

`kafka-up.sh` is not needed for any Phase 1 workflow: nothing in the
recovery loop reads or writes the event stream yet. It is required for the
E2 integration tests, which skip cleanly without it rather than failing.

## 4. Verify

```bash
# All pods Running, including ingress-nginx-controller
kubectl --context kind-helix-sandbox get pods -A

# payment-api through the ingress (200 expected)
curl --resolve payment-api.local:80:127.0.0.1 http://payment-api.local/health
curl --resolve payment-api.local:80:127.0.0.1 http://payment-api.local/ready

# Or with the hosts-file entry in place:
curl http://payment-api.local/health
```

## 5. Helix API + operator UI

```bash
cd <repo root>
uv sync
cp .env.example .env          # fill in HELIX_LLM_API_KEY at minimum
uv run helix serve            # binds 127.0.0.1:8787 by default
```

UI at http://127.0.0.1:8787/ui/. To reach it through the cluster ingress
at `http://helix.local` instead, the process must listen on a
non-loopback interface (the ingress controller is inside the cluster and
reaches back out via `host.docker.internal`):

```bash
HELIX_API_BIND=0.0.0.0 uv run helix serve
curl --resolve helix.local:80:127.0.0.1 http://helix.local/ui/
```

`helix.local` is an `ExternalName -> host.docker.internal` route
(`infra/helix-ingress.yaml`) - Docker Desktop only; the file's header
documents the Linux alternative (selectorless Service + manual Endpoints).

## 6. Optional stacks

Each is a committed compose file; bring up only what you need - this host
class cannot run kind + ELK + Jenkins + SonarQube simultaneously (CF-4).
Terraform can manage them instead via the `enable_*` flags.

```bash
# Elasticsearch + Kibana (log evidence, http://127.0.0.1:5601)
docker compose -f infra/docker-compose.yml up -d

# Jenkins (http://127.0.0.1:8080, admin/admin-sandbox-placeholder)
docker compose -f infra/jenkins-compose.yml up -d --build

# SonarQube (http://127.0.0.1:9000, admin/admin - heaviest, ~3GB)
wsl -d docker-desktop -u root sysctl -w vm.max_map_count=524288   # Windows only, once per DD restart
docker compose -f infra/sonarqube-compose.yml up -d
```

## 6.5. Phase 2 integrations (Slack / Jira / Notion / Neo4j)

All four are optional and independently configurable - unset credentials
record `skipped` on the integration outbox rather than failing runs, so
PostgreSQL remains the only hard dependency (CONTEXT.md §13).

```bash
# Slack (notifications + Approve/Reject buttons; 2.2)
HELIX_SLACK_BOT_TOKEN=xoxb-...        # chat:write on the channel
HELIX_SLACK_SIGNING_SECRET=...        # required for POST /slack/interactions
HELIX_SLACK_DEFAULT_CHANNEL=#helix-incidents
# Point Slack's Interactivity URL at https://<host>/slack/interactions -
# signature verification is the whole auth; an unset secret closes the
# surface (503) rather than accepting unsigned payloads.

# Work management (2.3/2.10) - pick one:
HELIX_WORK_PROVIDER=jira              # memory | jira | linear | trello
HELIX_JIRA_BASE_URL=https://you.atlassian.net
HELIX_JIRA_EMAIL=bot@you.com
HELIX_JIRA_API_TOKEN=...
HELIX_JIRA_PROJECT_KEY=HELIX
# or HELIX_LINEAR_API_KEY + HELIX_LINEAR_TEAM_ID,
# or HELIX_TRELLO_API_KEY + HELIX_TRELLO_API_TOKEN + HELIX_TRELLO_BOARD_ID.

# Notion knowledge pages (2.4)
HELIX_NOTION_API_KEY=ntn_...
HELIX_NOTION_PARENT_PAGE_ID=...       # pages are created under this parent

# Relationship graph (2.7/2.8) - Neo4j from infra/neo4j-compose.yml, which
# takes its password from this file:
#   docker compose -f infra/neo4j-compose.yml --env-file .env up -d
HELIX_GRAPH_ENABLED=true
HELIX_NEO4J_URI=bolt://127.0.0.1:7687
HELIX_NEO4J_USER=neo4j
HELIX_NEO4J_PASSWORD=...
```

Every transition's external side effects commit as `integration_outbox`
rows in the same transaction as the status change; `helix run` drains
them after each run and `helix sync` drains plus reconciles drift
(deleted Jira issues / Notion pages get re-created). Reviewer decisions
land via `helix review INC-* approve|reject`, `POST /reviews/{id}/...`,
or the Slack buttons on the review request.

### Who Helix is, and who may decide

`config/service_registry.yaml` names the live team: each service's owners,
reviewers, path owners and Kubernetes namespace (default: the service
name), and each person's display name and Slack member id
(`people.<handle>.slack`, from Slack: profile, three dots, Copy member ID).
Owners and reviewers are GitHub logins.

Only an owner of a service may approve its actions, only the requested
reviewer may decide a review, and a change's author decides neither. So:

- set `HELIX_OPERATOR` in `.env` to your own handle, or `helix approve`
  is refused;
- add your Slack member id under `people`, or your Slack clicks are
  refused (Slack tells you why);
- give every service you want Helix to act on a registry entry: a cluster
  write outside a registered namespace always needs approval.

## 7. The SonarQube quality gate (one-time token step)

The Jenkins `payment-api` pipeline has `SonarQube analysis` +
`Quality Gate` stages. The gate fails the build on a failed gate - but
both stages **skip** (log + continue) when the server is not reachable,
so the pipeline still works on a sonar-less host.

To enable it:

1. Start SonarQube (above) and wait for `docker ps` to show `healthy`
   (first boot takes a couple of minutes - DB migrations).
2. Log in at http://127.0.0.1:9000 (admin/admin, set a real password),
   then **My Account -> Security -> Generate Token**.
3. Put the token in `.env` as `SONARQUBE_TOKEN=<token>` (compose path) or
   `terraform.tfvars` as `sonarqube_token` (terraform path), and recreate
   Jenkins - JCasC injects it into the `sonarqube-token` credential at
   container start. The compose file's project directory is `infra/`, so
   it does not read the repo-root `.env` on its own - pass it explicitly:
   `docker compose -f infra/jenkins-compose.yml --env-file .env up -d`.
4. Create the quality-gate webhook (mandatory for `waitForQualityGate` -
   without it the gate stage waits until timeout): SonarQube
   **Administration -> Configuration -> Webhooks -> Create**, name
   `jenkins`, URL `http://host.docker.internal:8080/sonarqube-webhook/`.
   That is why both containers publish on all interfaces rather than
   127.0.0.1 - Docker Desktop does not forward loopback-only published
   ports to containers.
5. Run the `payment-api` Jenkins job - the analysis stage scans
   `sandbox/payment-api` (per `sonar-project.properties`, including the
   `coverage.xml` the Test stage now emits) and the gate stage enforces
   the server-side Quality Gate.

Verified wiring note: on the dev host this was exercised as far as JCasC
server/credential wiring and pipeline syntax; a full live scan needs
SonarQube and Jenkins running simultaneously, which exceeds this host's
memory headroom (CF-4) - the stages are guarded so the pipeline stays
green either way.

## 8. Teardown

```bash
# Everything terraform manages (cluster + containers it created):
cd infra/terraform && terraform destroy

# Or per-piece:
bash scripts/down.sh                                    # kind cluster
docker compose -f infra/sonarqube-compose.yml down
docker compose -f infra/jenkins-compose.yml down
docker compose -f infra/docker-compose.yml down
bash scripts/db-down.sh                                 # Postgres (or leave running)
```

Leave `helix-postgres` running between sessions - it holds Helix's
incident state and costs ~50MB. The kind cluster can also stay; ELK,
Jenkins and SonarQube should be torn down when not in use (CF-4).

## 9. What runs where (and why)

- **In the cluster**: payment-api + downstream-stub (the thing Helix
  breaks and repairs), ingress-nginx, filebeat/events shippers when the
  log stack is enabled.
- **On the host**: Postgres, Elasticsearch/Kibana, Jenkins, SonarQube,
  `helix serve`. These watch the cluster, so they must not share its
  failure domain (decisions D1/D3) - the fault catalogue can kill the
  cluster while observers stay up.
- **Ingress names** (`payment-api.local`, `helix.local`) map to
  127.0.0.1; the controller's NodePorts 30080/30443 are published back to
  the host by `infra/kind-config.yaml`'s extraPortMappings.
