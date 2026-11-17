# LinkedIn launch post (draft for #73)

Status: **draft**. Both owners must approve it, and it goes out only
after the new demo video (#68). No numbers, no claims beyond what the code
does: guardrails, the harness and the architecture decisions. Every decision
named here is recorded in `plan.md` (D1 to D27).

## Post

> We built Helix, an agent for DevOps recovery and change review. The model is the least interesting part of it. Most of the work went into what the model is not allowed to do, and into proving whether the agent was worth having at all.
>
> **Guardrails**
> - The model never gets a shell. It picks from a fixed catalogue of typed actions: restart, roll back, scale, rerun a pipeline, open a fix pull request.
> - Every action is checked against a safety policy twice: once when the coordinator routes it, and again inside the executor right before it runs. A bug in one layer cannot quietly skip the other.
> - Anything risky, or anything touching production, waits for an owner of that service. Nobody approves their own change without it being recorded as self-approved.
> - Helix opens fix pull requests. It never merges them.
> - It does not stop at "action ran". It verifies the system recovered, and hands the incident to a person when it did not.
>
> **The harness**
> Before trusting the agent we built a benchmark harness. Each scenario declares its root cause, valid actions, unsafe actions, owner and expected end state. The agent runs as a set of configurations, from plain rules to the full graph, and those are graph-composition settings, not code forks. A scripted approver stands in for the human, and bypassing an approval gate always scores as unsafe.
> It kept us honest. More architecture did not automatically mean better results, the plain rules were a strong baseline, and the harness caught the knowledge graph making ownership worse until we fixed it.
>
> **Decisions we would make again**
> - Postgres is the source of truth. Slack, Jira and Notion are projections, fed through a transactional outbox, so a vendor outage never loses a decision.
> - Slack is a door, not a store. Approvals arrive there, but they are checked and recorded in Postgres.
> - CI and Helix's own state live outside the cluster being broken, so they never share its failure domain.
> - Ingesting events never executes anything. Acting is a separate, explicit step.
> - The knowledge graph is a derived projection, off by default, rebuilt from Postgres.
> - Vendors are reached through capability contracts, never from inside the agent's reasoning.
> - No signed-in person, no decision. A machine token cannot approve anything on someone's behalf.
> - When the model asks for a riskier action than it needs, we report it as a finding, not a failure. The policy catches it, and hiding it would hide the most useful thing the harness tells us.
> - We kept the benchmark on a local cluster on purpose. A managed cloud upgrading itself mid-run would have become the thing we measured.
>
> Code: https://github.com/nikhilpravinpise/Helix
> Demo: [video link]
>
> If you run CI or Kubernetes for a team and want to try it, comment or DM us. We would rather hear what breaks.
>
> Built with [@Nikhil Pise]

## Alternate hooks

1. "Our agent's most important feature is the list of things it is not allowed to do."
2. "Before trusting our DevOps agent, we built a harness that could prove it was not worth having. That changed how we built it."

## Hashtags

#DevOps #AIAgents #SRE #LangGraph #SoftwareArchitecture #OpenSource

## Visual

First choice: the new demo video (#68), uploaded natively with captions.
Fallback: the architecture diagram, `docs/architecture/helix-architecture.svg`
exported to PNG.

## Link plan

- Repository: https://github.com/nikhilpravinpise/Helix (public).
- Demo video: the #68 upload, also linked from the top of the README.
- Put the repository link in the post itself, not only in a comment.

## Still to decide

| Item | Owner | Decision |
|---|---|---|
| Which account publishes, who reshares | Vraj and Nikhil | |
| Date and time | Vraj and Nikhil | |
| Tagging Nikhil and testers | Ask each person first | |
| Final text approved | Vraj and Nikhil | |

## Before publishing

- [ ] #68 video is up, and its link replaces `[video link]`.
- [ ] Both owners approved the text above.
- [ ] Everyone tagged has agreed to it.
- [ ] After posting, link the post from the README (#73).
