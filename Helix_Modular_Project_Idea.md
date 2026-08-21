**HELIX**

**A Modular Agentic DevOps Platform**

From Evidence-Constrained DevOps Recovery to Event-Driven Engineering Lifecycle Coordination

| **Phase**               | **Focus**                                                                                       |
| ----------------------- | ----------------------------------------------------------------------------------------------- |
| Phase 1 - MVP           | Evidence-based CI/CD and runtime incident diagnosis, safe remediation, validation, and recovery |
| Phase 1.5 - Event Layer | Kafka, canonical EngineeringEvent, correlation, event history, and decoupled integrations       |
| Phase 2 - Full Helix    | Triage, review, ops, knowledge, Slack, Jira, Notion, Neo4j/GraphRAG, optional Linear and Trello |

B.Tech CSE (Data Science) | DevOps Laboratory | DJS23OLOE501

# 1\. Executive Summary

Helix is designed as a modular system with a strong Phase 1 and a larger Phase 2. Phase 1 is the earlier Helix idea: an evidence-constrained DevOps recovery system that consumes CI/CD and runtime signals, investigates failures using GitHub, Jenkins, Kubernetes, Docker, and ELK evidence, proposes a safe recovery action, validates it, executes it when allowed, and verifies the result.

Phase 1.5 adds Kafka and a canonical EngineeringEvent model. This separates event collection from agent reasoning and creates a clean path to more integrations.

Phase 2 extends the same core into an engineering lifecycle platform. It adds Triage, Review, Ops, and Knowledge agents; Slack for communication and approval; Jira for work tracking; Notion for durable incident knowledge; Neo4j and GraphRAG for relationship context; and optional Linear and Trello connectors behind a shared Work Management interface.

# 2\. Why Modular

| **Problem**                  | **Modular response**                                                                        |
| ---------------------------- | ------------------------------------------------------------------------------------------- |
| Too many moving parts        | Each phase has a complete end-to-end path.                                                  |
| Hard to debug                | A failure can be isolated to one phase.                                                     |
| Tight vendor coupling        | Integrations use capability interfaces and, from Phase 1.5, normalized events.              |
| Premature architecture       | Kafka, Neo4j, and provider abstractions are introduced only when the next phase needs them. |
| Hard to demonstrate progress | Each phase has its own demo and metrics.                                                    |

# 3\. Phase 1 - Helix MVP

GitHub  
|  
v  
Jenkins  
|  
+----> Docker build/test  
|  
v  
Kubernetes deployment  
|  
+----> ELK logs  
|  
v  
Helix  
+--> Diagnose  
+--> Plan  
+--> Validate  
+--> Recover  
+--> Verify

| **Capability** | **Phase 1 behavior**                                                                  |
| -------------- | ------------------------------------------------------------------------------------- |
| Detection      | Receive CI/CD or runtime failure signals.                                             |
| Evidence       | Read recent GitHub changes, Jenkins logs, Kubernetes state, container logs, ELK logs. |
| Diagnosis      | Form a root-cause hypothesis from collected evidence.                                 |
| Planning       | Generate and rank possible remediation actions.                                       |
| Validation     | Run tests, dry-runs, or isolated checks when practical.                               |
| Execution      | Run bounded actions or create a GitHub PR.                                            |
| Verification   | Check whether the system actually recovered.                                          |

# 4\. Phase 1 Investigation Example

PR #184  
|  
v  
GitHub change  
|  
v  
Jenkins deployment fails  
|  
v  
Helix starts investigation  
|  
+--> GitHub: changed files and diff  
+--> Jenkins: failing stage and logs  
+--> Kubernetes: pod status/events  
+--> ELK: application errors  
|  
v  
Evidence chain  
|  
v  
Root-cause hypothesis  
|  
v  
Candidate actions  
1\. restart  
2\. rollback  
3\. configuration fix  
|  
v  
Validation -> Execute -> Verify

Monitoring is not the same as diagnosis. The event system surfaces the failure. The agent then queries the systems that can provide evidence and forms a hypothesis.

# 5\. Phase 1 Agents

| **Component**      | **Job**                                                                     |
| ------------------ | --------------------------------------------------------------------------- |
| Coordinator        | Keep incident state and decide what investigation step runs next.           |
| Diagnosis Agent    | Collect and interpret evidence.                                             |
| Recovery Planner   | Generate candidate actions and compare evidence, risk, and expected effect. |
| Validation Agent   | Test the selected recovery before execution when practical.                 |
| Verification Agent | Check the desired post-action state.                                        |

# 6\. Phase 1 Access and Safety

| **Access**        | **Examples**                                                 | **Default**           |
| ----------------- | ------------------------------------------------------------ | --------------------- |
| Read              | Repo, commits, PRs, Jenkins results, K8s state, logs.        | Allowed               |
| Low-risk write    | Create branch/PR, restart disposable pod.                    | Allowed with checks   |
| Medium-risk write | Staging rollback, scaling.                                   | Policy + verification |
| High-risk write   | Production mutation, secret changes, destructive operations. | Human approval        |

# 7\. Phase 1.5 - Event-Driven Helix

Kafka is introduced after the Phase 1 direct path works. It is used to decouple producers and consumers, normalize events, support asynchronous workflows, and preserve event history.

GitHub ----Jenkins -----> Event Adapters -> Kafka -> Helix State -> Agents  
Kubernetes -/  
ELK --------/

The first canonical event model should remain small.

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

| **Family**     | **Examples**                                                                                          |
| -------------- | ----------------------------------------------------------------------------------------------------- |
| Source control | push.created, pr.opened, pr.updated, pr.approved, pr.rejected, pr.merged                              |
| CI/CD          | build.started, build.passed, build.failed, deployment.started, deployment.failed                      |
| Runtime        | pod.failed, rollout.failed, service.unhealthy                                                         |
| Helix          | incident.created, remediation.requested, approval.requested, remediation.completed, incident.resolved |

# 8\. Phase 2 - Full Helix

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

Phase 2 does not replace Phase 1 recovery. It adds lifecycle coordination around the same core.

# 9\. Phase 2 Agents

| **Agent**       | **Question**                                        | **Integrations**                           |
| --------------- | --------------------------------------------------- | ------------------------------------------ |
| Coordinator     | What should happen next?                            | Kafka, PostgreSQL, all agents              |
| Triage Agent    | What happened, what is affected, and who owns it?   | GitHub, Jira, Notion, Neo4j                |
| Review Agent    | Who should review this change and why?              | GitHub, Neo4j, Slack                       |
| Ops Agent       | What failed and what is the safest recovery action? | Jenkins, Kubernetes, Docker, ELK, GitHub   |
| Knowledge Agent | What does the organization know about this system?  | Neo4j, GitHub, Jira, Notion, event history |

# 10\. Core Phase 2 Integrations

| **Integration** | **Role**                                            | **Direction**          |
| --------------- | --------------------------------------------------- | ---------------------- |
| GitHub          | Code, PRs, review state, changes, controlled fixes. | Events + API           |
| Jenkins         | CI/CD execution and evidence.                       | Events + API           |
| Kubernetes      | Runtime state and controlled remediation.           | Events + API           |
| ELK             | Runtime evidence.                                   | Queries                |
| Slack           | Communication, approval, escalation.                | Events + messages      |
| Jira            | Work and incident tracking.                         | Events + issue actions |
| Notion          | Durable incident knowledge.                         | Create/update pages    |

# 11\. Optional Linear and Trello

Linear and Trello are optional Work Management providers. They should not become separate agents. The Helix agents should depend on a provider-neutral interface.

WorkManagementProvider  
|  
+-- JiraProvider  
+-- LinearProvider  
+-- TrelloProvider

| **Capability**   | **Jira**    | **Linear**      | **Trello** |
| ---------------- | ----------- | --------------- | ---------- |
| Create work item | Issue       | Issue           | Card       |
| Assign           | Assignee    | Assignee        | Member     |
| Status           | Workflow    | State           | List       |
| Comment          | Comment     | Comment         | Comment    |
| Link evidence    | Issue links | Relations/links | Card links |

# 12\. Example Phase 2 Lifecycle

**1\.** Developer opens GitHub PR #184.

**2\.** GitHub emits pr.opened and Kafka transports the event.

**3\.** Triage Agent identifies the affected service and owner.

**4\.** Review Agent selects a reviewer using ownership and review history.

**5\.** Slack receives a targeted review request.

**6\.** Jenkins runs the pipeline.

**7\.** The pipeline fails and emits build.failed.

**8\.** Ops Agent collects GitHub, Jenkins, Kubernetes, and ELK evidence.

**9\.** Helix creates or updates the Jira incident and Notion incident page.

**10\.** A risky recovery action requests approval in Slack.

**11\.** A persistent code/configuration fix is created as a GitHub branch and PR.

**12\.** Jenkins validates the fix.

**13\.** Kubernetes and ELK verify recovery.

**14\.** Jira moves to resolved, Notion is updated, and Slack gets the final result.

# 13\. Neo4j and GraphRAG

Neo4j belongs in Phase 2 because relationship-heavy questions appear only after the basic incident loop works.

Developer --OWNS--> Service  
Developer --REVIEWS--> Repository  
Service --DEPENDS_ON--> Database  
PR --MODIFIES--> Service  
PR --CAUSED--> Deployment  
Incident --INVOLVES--> Service  
Developer --RESOLVED--> Incident  
Incident --TRACKED_BY--> Jira  
Incident --DOCUMENTED_IN--> Notion

Examples: Who owns PaymentService? Who should review a PR if the owner authored it? Who fixed a similar incident? Which PR caused this deployment?

# 14\. Novelty Positioning

Do not claim novelty from the individual technologies. Existing work already covers major parts of event-driven DevOps automation, multi-agent SRE, GraphRAG, GitHub automation, and team-tool integrations.

- The modular evolution from a verified recovery system to a broader engineering lifecycle system.
- A canonical EngineeringEvent model that connects technical and organizational systems.
- A shared engineering state that links changes, builds, deployments, incidents, owners, reviewers, and outcomes.
- Decision provenance from trigger -> evidence -> candidate action -> selected action -> validation -> outcome.
- An evaluation plan that compares Phase 1, event-driven, and graph-grounded versions.

# 15\. Evaluation Plan

| **Phase**      | **Main metrics**                                                                                                |
| -------------- | --------------------------------------------------------------------------------------------------------------- |
| Phase 1        | RCA accuracy, action validity, recovery success, MTTR, unsafe action rate                                       |
| Phase 1.5      | Event correlation accuracy, duplicate handling, event latency, replay correctness                               |
| Phase 2        | Owner accuracy, reviewer selection accuracy, lifecycle consistency, duplicate work-item rate, evidence coverage |
| Provider study | Same workflow using Jira, Linear, and Trello without changing agent logic                                       |

# 16\. Controlled Benchmark

Start with 15 to 20 reproducible scenarios.

| **Category**   | **Examples**                                                                                    |
| -------------- | ----------------------------------------------------------------------------------------------- |
| CI/CD          | Dependency failure, test regression, Docker build failure, environment mismatch                 |
| Kubernetes     | CrashLoopBackOff, OOMKilled, failed rollout, missing Secret                                     |
| GitHub         | Rejected PR, failed check, stale branch, bad deployment configuration                           |
| Organizational | Missing owner, wrong reviewer, duplicate incident, stale work item                              |
| Cross-system   | PR causes deployment failure, runtime failure with missing owner, Jira/Notion state out of sync |

# 17\. Technology Roadmap

| **Technology** | **Phase**  | **Role**                                             |
| -------------- | ---------- | ---------------------------------------------------- |
| GitHub         | 1+         | Source of change, review, evidence, controlled fixes |
| Jenkins        | 1+         | CI/CD execution and evidence                         |
| Docker         | 1+         | Containers and validation                            |
| Kubernetes     | 1+         | Runtime and controlled recovery                      |
| ELK            | 1+         | Logs and investigation evidence                      |
| LangGraph      | 1+         | Stateful agent workflows                             |
| PostgreSQL     | 1+         | Canonical active state                               |
| Kafka          | 1.5+       | Event backbone and event history                     |
| Slack          | 2          | Communication and approval                           |
| Jira           | 2          | Work and incident tracking                           |
| Notion         | 2          | Durable incident knowledge                           |
| Neo4j          | 2          | Relationship graph                                   |
| GraphRAG       | 2          | Relationship-aware retrieval                         |
| Linear         | 2 optional | Alternative work-management provider                 |
| Trello         | 2 optional | Alternative work-management provider                 |
| Terraform      | 1/2        | Infrastructure as Code                               |
| Ansible        | 1/2        | Configuration management                             |
| Selenium       | 1/2        | E2E validation                                       |
| SonarQube      | 1+         | Quality gate                                         |
| PyCaret        | Optional   | MLOps extension                                      |

# 18\. DevOps Laboratory Mapping

| **Unit**           | **Helix coverage**                            |
| ------------------ | --------------------------------------------- |
| 1\. Docker         | Containerization and validation workloads     |
| 2\. Terraform      | Reproducible infrastructure                   |
| 3\. Puppet/Ansible | Configuration and node setup                  |
| 4\. Jenkins        | Build, test, quality, deploy, secret handling |
| 5\. Selenium       | E2E deployment verification                   |
| 6\. Scripting      | Python/Bash adapters and automation           |
| 7\. Kubernetes     | Deploy, scale, restart, rollback              |
| 8\. Ingress        | Expose application and Helix interfaces       |
| 9\. ELK            | Logs and troubleshooting evidence             |
| 10\. PyCaret       | Optional MLOps extension                      |

# 19\. Suggested Repository Structure

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

# 20\. Engineering Principles

- Phase 1 must be useful and demoable without Phase 2 integrations.
- Phase 2 must extend Phase 1 rather than replace it.
- Add abstractions when a repeated need appears.
- Do not add integrations only for architecture diagrams.
- Use GitHub PRs for persistent code/configuration changes when practical.
- Use least privilege and approval for high-risk operations.
- Slack is communication; Jira/Linear/Trello are work management; Notion is durable knowledge.
- Do not make vendor APIs part of agent reasoning.
- Do not add Neo4j or GraphRAG in Phase 1.
- Do not add MCP or a large tool-control layer before a real reuse or decoupling need is demonstrated.
- Prefer a small number of reliable agents over a large swarm.
- Reproduce bugs E2E before fixing them and treat lint, test failures, and flakiness as defects.

# 21\. Global Agent Instructions

- Never use the em dash character. Use plain dash "-" instead.
- When writing commit messages, NEVER auto-add your agent name as co-author.
- Never manually modify CHANGELOG.md files or any files that are marked as auto-generated.
- When making technical decisions, do not give much weight to development cost. Prefer quality, simplicity, robustness, scalability, and long term maintainability.
- For one-off or infrequent operational work, start with the simplest direct end-to-end path. Do not build wrappers, control planes, policy layers, custom verifiers, or automation unless the direct path exposes a concrete blocker or repeated need that justifies the added machinery.
- When doing bug fixes, always start with reproducing the bug in an E2E setting as closely aligned with how an end user would experience it as possible.
- When end-to-end testing a product, be picky about the UI and aim for pixel perfection. Fix obvious visual defects along the way.
- Apply the same standard to engineering excellence: lint, test failures, and test flakiness should be fixed rather than ignored.
- Before using "dynamic workflows", "ultra code" or any harness feature that immediately spawns a large swarm of subagents, explain the tradeoffs and ask for explicit approval.

# 22\. Final Definition

**Helix:** a modular agentic DevOps platform that starts with evidence-constrained incident recovery and grows into an event-driven engineering lifecycle system.