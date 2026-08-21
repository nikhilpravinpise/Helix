# Helix - Repository Context

## 1. Identity

**Name:** Helix

**Positioning:** A modular agentic DevOps platform that starts with evidence-constrained incident recovery and grows into an event-driven engineering lifecycle system.

**Phase 1:** Incident diagnosis, safe remediation, validation, and recovery.

**Phase 1.5:** Kafka event backbone, canonical EngineeringEvent, correlation, and event history.

**Phase 2:** Triage, review, operations, knowledge, Slack, Jira, Notion, Neo4j/GraphRAG, and optional Linear/Trello work-management providers.

---

## 2. Core Strategy

Build in phases. Phase 1 must work on its own. Phase 2 must extend the same core instead of replacing it.

```text
Phase 1
Evidence-Constrained DevOps Recovery
        |
        v
Phase 1.5
Event-Driven Helix
        |
        v
Phase 2
Agentic Engineering Lifecycle Platform
```

Do not build the final Phase 2 architecture during Phase 1 unless a concrete blocker requires part of it.

---

## 3. Phase 1 MVP

Phase 1 is the original Helix recovery system.

```text
GitHub -> Jenkins -> Docker/Kubernetes -> ELK
                           |
                           v
                         Helix
             Diagnose -> Plan -> Validate
                 -> Recover -> Verify
```

### Phase 1 jobs

- Receive CI/CD or runtime failure signals.
- Build an incident record.
- Collect evidence from GitHub, Jenkins, Kubernetes, Docker, and ELK.
- Produce a root-cause hypothesis.
- Generate candidate recovery actions.
- Compare evidence, risk, and expected effect.
- Validate actions when practical.
- Execute bounded actions or create a GitHub PR.
- Verify the desired post-action state.
- Record outcome and evidence.

### Important distinction

Monitoring surfaces the failure.

The agent investigates it.

```text
Events
  -> say something happened

Evidence tools
  -> provide facts

Agents
  -> interpret facts

Planner
  -> selects an action

Validation
  -> checks the action

Execution
  -> changes the system

Verification
  -> checks recovery
```

Do not describe Helix as an LLM that continuously reads every log.

---

## 4. Phase 1 Agents

- **Coordinator:** owns workflow state and chooses the next investigation step.
- **Diagnosis Agent:** gathers and interprets evidence.
- **Recovery Planner:** generates and ranks candidate actions.
- **Validation Agent:** tests proposed actions.
- **Verification Agent:** checks whether the desired state was restored.

Keep Phase 1 agent count small.

---

## 5. Phase 1 Access

### Read

- GitHub repository, commits, diffs, PRs, checks
- Jenkins builds, logs, test results
- Kubernetes resources and events
- Docker container/image state
- ELK logs

### Controlled write

- create GitHub branch
- commit/PR for persistent fixes
- low-risk runtime action in non-production

### High risk

- production mutation
- secret changes
- destructive infrastructure operations

Require human approval.

Never expose arbitrary shell access to the LLM.

---

## 6. Phase 1.5 - Kafka

Kafka is introduced after the direct Phase 1 workflow is stable.

Its purpose is:

- decouple producers and consumers
- normalize events
- support asynchronous workflows
- preserve event history
- make additional integrations easier
- support replay and correlation

Architecture:

```text
GitHub ----Jenkins ----Kubernetes ---> adapters -> Kafka -> Helix State -> Agents
ELK --------/
```

Do not add Kafka only because it looks good in an architecture diagram.

---

## 7. Canonical EngineeringEvent

All external events should eventually map to:

```json
{
  "event_id": "evt_123",
  "event_type": "deployment.failed",
  "source": "kubernetes",
  "timestamp": "...",
  "correlation_id": "INC-1042",
  "service": "payment-api",
  "environment": "staging",
  "actor": "system",
  "metadata": {}
}
```

Start with a small event taxonomy:

```text
push.created
pr.opened
pr.updated
pr.approved
pr.rejected
pr.merged
build.started
build.passed
build.failed
deployment.started
deployment.failed
deployment.succeeded
pod.failed
incident.created
remediation.requested
approval.requested
remediation.completed
incident.resolved
```

Expand only when a real workflow needs another event.

---

## 8. Phase 2 - Full Helix

Phase 2 keeps the recovery system and adds lifecycle coordination.

```text
                            Kafka
                              |
                        Helix State
                              |
              +---------------+----------------+
              |                |                |
           Triage            Review             Ops
            Agent             Agent             Agent
              |                |                |
              +---------------+----------------+
                              |
                       Knowledge Agent
                              |
              +---------------+----------------+
              |                |                |
            Slack             Jira             Notion
              |                |                |
        communication         work           knowledge
                               |
                        Work Management
                               |
                    +----------+----------+
                    |                     |
                 Linear                Trello
                (optional)            (optional)
```

---

## 9. Phase 2 Agents

### Coordinator

Answers: What should happen next?

Owns state transitions, retries, escalation, and duplicate-action prevention.

### Triage Agent

Answers: What happened, what is affected, and who owns it?

Uses GitHub, Jira, Notion, and Neo4j.

### Review Agent

Answers: Who should review this change and why?

Uses GitHub, ownership/review history, Neo4j, and Slack.

### Ops Agent

Answers: What failed and what is the safest recovery action?

Uses Jenkins, Kubernetes, Docker, ELK, and GitHub.

### Knowledge Agent

Answers: What does the organization know about this system?

Uses Neo4j, GitHub, Jira, Notion, and event history.

---

## 10. Core Integrations

### GitHub

GitHub is the source-of-change and review system.

It provides:

- files
- commits
- diffs
- PRs
- reviews
- checks
- events

It also provides the controlled path for persistent code/configuration changes:

```text
branch -> commit -> PR -> review -> merge
```

Do not push directly to protected branches from the agent.

### Jenkins

CI/CD execution and evidence.

### Kubernetes

Runtime state and controlled remediation.

### ELK

Runtime logs and investigation evidence.

### Slack

Human communication, approval, escalation, and status.

Slack is not the canonical database.

### Jira

Work and incident management.

### Notion

Durable human-readable incident knowledge.

---

## 11. Optional Linear and Trello

Linear and Trello are optional Work Management providers.

They must implement a shared provider contract.

```text
WorkManagementProvider
    |
    +-- JiraProvider
    +-- LinearProvider
    +-- TrelloProvider
```

Common capabilities:

```text
create_work_item
update_work_item
assign_work_item
transition_work_item
comment_work_item
link_work_item
```

Agents should use the provider-neutral contract.

They must not contain Jira-specific or Linear-specific workflow logic.

Internal state should use provider-neutral concepts:

```text
WorkItem
Status
Assignee
Priority
Project
Link
Comment
```

---

## 12. Neo4j and GraphRAG

Neo4j belongs in Phase 2.

Use it when the project needs relationship-heavy context such as:

```text
Developer --OWNS--> Service
Developer --REVIEWS--> Repository
Service --DEPENDS_ON--> Database
PR --MODIFIES--> Service
PR --CAUSED--> Deployment
Incident --INVOLVES--> Service
Developer --RESOLVED--> Incident
Incident --TRACKED_BY--> Jira
Incident --DOCUMENTED_IN--> Notion
```

Questions this can support:

- Who owns PaymentService?
- Who should review this PR if the owner authored it?
- Who fixed a similar incident?
- Which PR caused this deployment?
- Which incidents are linked to this service?

Do not add Neo4j or GraphRAG to Phase 1.

---

## 13. Shared Engineering State

PostgreSQL is the canonical active state store.

Example:

```json
{
  "incident_id": "INC-1042",
  "service": "payment-api",
  "status": "awaiting_approval",
  "owner": "rahul",
  "pr": 184,
  "deployment": 72,
  "severity": "high",
  "attempt": 2
}
```

External systems are integration surfaces and projections around this state.

---

## 14. Unhappy Paths

Unhappy paths are first-class.

Example:

```text
TRIAGED
  -> OWNER_ASSIGNED
  -> REVIEW_PENDING
  -> REJECTED
  -> FIX_REQUIRED
  -> REVIEW_PENDING
  -> APPROVED
  -> DEPLOYING
  -> FAILED
  -> DIAGNOSING
  -> REMEDIATION_PENDING
  -> EXECUTING
  -> VALIDATING
  -> RECOVERED
```

Rules:

- failed validation should cause replanning
- do not blindly repeat the same action
- bound retries
- escalate when confidence or retry limits are reached
- record transitions

---

## 15. Engineering Event Flow

The full Phase 2 loop is:

```text
EVENT
  -> CORRELATE
  -> UNDERSTAND
  -> PLAN
  -> ACT
  -> VERIFY
  -> UPDATE STATE
  -> EMIT EVENT
  -> loop
```

The system should make it possible to trace:

```text
Trigger
  -> Evidence
  -> Hypothesis
  -> Candidate actions
  -> Selected action
  -> Validation
  -> Execution
  -> Outcome
```

This is decision provenance.

---

## 16. Phase 2 Example

```text
GitHub PR opened
      |
      v
Kafka event
      |
      v
Triage -> owner
      |
      v
Review -> reviewer
      |
      v
Slack -> review request
      |
      v
Jenkins -> build.failed
      |
      v
Ops Agent -> GitHub + Jenkins + K8s + ELK evidence
      |
      v
Jira + Notion -> incident records
      |
      v
Recovery decision
      |
      +--> approval via Slack when required
      |
      v
GitHub PR for persistent fix
      |
      v
Jenkins validation
      |
      v
Kubernetes + ELK verification
      |
      v
Jira resolved + Notion updated + Slack notified
```

---

## 17. Evaluation

### Phase 1

- RCA accuracy
- action validity
- recovery success
- MTTR
- unsafe action rate

### Phase 1.5

- event correlation accuracy
- duplicate handling
- event processing latency
- replay correctness

### Phase 2

- owner identification accuracy
- reviewer selection accuracy
- lifecycle consistency
- duplicate work-item rate
- decision evidence coverage
- human intervention rate

### Optional provider study

Run the same workflow with:

```text
Jira
Linear
Trello
```

without changing agent logic.

---

## 18. Controlled Benchmark

Start with 15 to 20 reproducible scenarios.

Categories:

- CI/CD: dependency failure, test regression, Docker build failure, configuration mismatch
- Kubernetes: CrashLoopBackOff, OOMKilled, failed rollout, missing Secret
- GitHub: rejected PR, failed check, stale branch, bad deployment configuration
- Organizational: missing owner, wrong reviewer, duplicate incident, stale work item
- Cross-system: PR causes deployment failure, runtime incident with missing owner, Jira/Notion state out of sync

Each scenario should have:

```text
root cause
valid actions
unsafe actions
owner
optimal action
expected final state
```

---

## 19. Ablation

Compare progressively richer versions:

```text
A - rules only
B - single LLM agent
C - LLM + RAG
D - event + graph context
E - full Helix + verification
```

The objective is to measure whether the additional architecture improves results. More components do not automatically mean better results.

---

## 20. Technology Roadmap

| Technology | Phase |
|---|---|
| GitHub | 1+ |
| Jenkins | 1+ |
| Docker | 1+ |
| Kubernetes | 1+ |
| ELK | 1+ |
| LangGraph | 1+ |
| PostgreSQL | 1+ |
| Kafka | 1.5+ |
| Slack | 2 |
| Jira | 2 |
| Notion | 2 |
| Neo4j | 2 |
| GraphRAG | 2 |
| Linear | 2 optional |
| Trello | 2 optional |
| Terraform | 1/2 |
| Ansible | 1/2 |
| Selenium | 1/2 |
| SonarQube | 1+ |
| PyCaret | optional |

---

## 21. Repository Structure

```text
helix/
├── agent/
│   ├── graph.py
│   ├── state.py
│   ├── agents/
│   └── tools/
├── integrations/
│   ├── github/
│   ├── jenkins/
│   ├── kubernetes/
│   ├── elk/
│   ├── slack/
│   ├── jira/
│   ├── notion/
│   ├── linear/
│   └── trello/
├── events/
│   ├── schemas.py
│   └── adapters/
├── state/
├── graph/
├── ui/
├── infra/
├── tests/
├── scripts/
├── docs/
├── CONTEXT.md
└── README.md
```

Integration code stays inside `integrations/`.

Agents depend on capability interfaces.

---

## 22. Engineering Principles

- Phase 1 must be useful without Phase 2.
- Phase 2 must extend Phase 1.
- Add abstractions only when a repeated need appears.
- Do not add integrations for visual architecture alone.
- Prefer GitHub PRs for persistent code/configuration changes.
- Use least privilege.
- High-risk production actions require approval.
- Do not expose arbitrary shell access to the LLM.
- Do not make vendor APIs part of agent reasoning.
- Do not add Neo4j or GraphRAG in Phase 1.
- Do not add MCP or a large tool-control layer before a concrete reuse or decoupling need exists.
- Prefer a small number of reliable agents over a large swarm.
- Reproduce bugs E2E before fixing them.
- Treat lint failures, test failures, and flakiness as defects.

---

## 23. Global Agent Instructions

- Never use the em dash character. Use plain dash "-" instead.
- When writing commit messages, NEVER auto-add your agent name as co-author.
- Never manually modify CHANGELOG.md files or any files that are marked as auto-generated.
- When making technical decisions, do not give much weight to development cost. Prefer quality, simplicity, robustness, scalability, and long term maintainability.
- For one-off or infrequent operational work, start with the simplest direct end-to-end path. Do not build wrappers, control planes, policy layers, custom verifiers, or automation unless the direct path exposes a concrete blocker or repeated need that justifies the added machinery.
- When doing bug fixes, always start with reproducing the bug in an E2E setting as closely aligned with how an end user would experience it as possible.
- When end-to-end testing a product, be picky about the UI and aim for pixel perfection. Fix obvious visual defects along the way.
- Apply the same standard to engineering excellence: lint, test failures, and test flakiness should be fixed rather than ignored.
- Before using "dynamic workflows", "ultra code" or any harness feature that immediately spawns a large swarm of subagents, explain the tradeoffs and ask for explicit approval.

---

## 24. Final Definition

> Helix is a modular agentic DevOps platform that starts with evidence-constrained incident recovery and grows into an event-driven engineering lifecycle system.

Phase 1 proves recovery.

Phase 1.5 proves the event model.

Phase 2 proves cross-system engineering coordination.

Jira, Notion, and Slack are core Phase 2 integrations.

Linear and Trello are optional providers behind a shared Work Management interface.
