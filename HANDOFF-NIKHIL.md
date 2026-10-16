# Handoff - To Nikhil: Helix as a product

**Date:** 2026-10-16
**Branch:** `main` at `aea4de3` (`VrajVed/Helix`). Everything up to PR #46
is merged into `nikhilpravinpise/Helix`; PR #47 (one small fix) is open.
**Tests:** 2301 unit pass, 51 integration deselected by default, lint clean
(`make lint && make test`, see [docs/verifying.md](docs/verifying.md))
**Live:** https://helix-eksf.onrender.com (Render, free tier)
**Prior handoffs:** [HANDOFF-PHASE2-EXIT.md](HANDOFF-PHASE2-EXIT.md) |
[HANDOFF-VRAJ.md](HANDOFF-VRAJ.md) | [HANDOFF-FIXES.md](HANDOFF-FIXES.md) |
[HANDOFF-PHASE2.md](HANDOFF-PHASE2.md) | [HANDOFF-PHASE1.5.md](HANDOFF-PHASE1.5.md) |
[HANDOFF-PHASE1.md](HANDOFF-PHASE1.md)

@nikhilpravinpise this is the one document to read first. It covers
everything since the Phase 2 exit handoff (2026-10-09): what we decided,
what changed, how the hosted app works, what is open on GitHub, and where
the product is going. Older detail lives in the docs it links to.

---

## 1. Start here

**What Helix is now.** Phases 1, 1.5 and 2 are done and measured (see
[HANDOFF-PHASE2-EXIT.md](HANDOFF-PHASE2-EXIT.md)). Since then the hosted
web app on Render became the product: a team signs in with GitHub,
connects Slack, Jira and GitHub from the dashboard, manages its people and
services there, and approves Helix's actions from the dashboard or Slack.
Laptop setups (`.env`, Socket Mode, `start.sh`) are only how we develop
Helix, not how anyone uses it.

**Do these first.**

1. **Merge PR #47** (HEAD support for uptime monitors, already deployed).
2. **Sign in** at https://helix-eksf.onrender.com. You are a **co-owner**;
   VrajVed is the **main owner**. Check that the Team page shows both of you
   and the Services page shows `payment-api`.
3. **Read the open issues** (section 7) and answer the ones marked
   "decision": #41, #42, #45.
4. **Note the deadline:** the free Render Postgres (`helix-db`) **expires on
   2026-11-13**. Before then, upgrade it to a paid plan or move the data,
   or every incident, team member, service and connection is lost.

---

## 2. What changed since the Phase 2 exit (PRs #31 to #47)

| PR | Merged | What it did |
|---|---|---|
| #31 | 10-11 | Unit tests ignore the developer's `.env`; `latest.*` reports protected from partial runs; provider rate limits skipped cleanly |
| #32 | 10-14 | Pinned the helix-state Docker subnet so it stops shadowing campus Wi-Fi DNS |
| #33 | 10-14 | Closed the last audit findings: crash vs. PR correlation (E1b), billing-worker evidence (B3), vendor event times (E4) |
| #34, #35 | 10-14 | **OPT-1 done**: advisory PyCaret model, trained and versioned through Jenkins, judged honestly ([mlops/](mlops/)) |
| #36 | 10-15 | Hosted Helix on Render behind Sign in with GitHub |
| #37 | 10-15 | Render behaves like local: OAuth connections (Slack, Jira), GitHub App PR bot, PR bot settings |
| #39 | 10-15 | Product DX: public landing page, dashboard home, links out to Slack/Jira/GitHub, plain-English Connections page, draggable sidebar, no demo framing |
| #40 | 10-15 | **Team and services moved out of the YAML into the dashboard** (details in section 4) |
| #46 | 10-15 | **Owner, admin and member roles**, and service "owners" renamed **Approvers** |
| #47 | open | `/`, `/login` and `/healthz` answer HEAD, so UptimeRobot sees Helix as up |

The plan and decision record for #39, #40 and #46 is
[future_plans.md](future_plans.md), sections "Product DX", "Next: team and
services in the dashboard" and "Roles: owners, admins, members".

---

## 3. Decisions (made by Vraj; carry them forward)

### Product and hosting

- **The hosted web app is the product**, built for another team's
  developer. No "demo" badge, no boot seeding; "Send a test incident"
  checks the connected tools end to end.
- Render is the hosting for now. **GCP is still required by the course**
  (DJS23OLOE501, plan.md D23) and is deferred by decision, not done
  ([infra/gcp/README.md](infra/gcp/README.md)).
- Fix PRs go to the throwaway repo `VrajVed/helix-sandbox` and are never
  merged by Helix. A PR cap is optional; the PR bot is configurable on
  the Connections page instead.

### Connections

- Slack and Jira connect with **OAuth** from the Connections page; GitHub
  through the **GitHub App** "Helix Incident Bot" (not an OAuth app).
  Tokens are stored encrypted (`HELIX_CONNECTIONS_KEY`), never in git.
- Only one Slack app should exist for the product (issue #38 explains the
  two apps we have today and how to retire one).

### Team, services and sign-in

- **The database is the source of truth** for the team and services.
  `config/service_registry.yaml` was imported **once** at first boot
  (`helix team import`, only into an empty team); the dashboard is now the
  only editor. The benchmark keeps its own fixed
  `benchmark/service_registry.yaml`.
- Rule in code: `HELIX_SERVICE_REGISTRY` set means read that file
  (benchmark, tests); unset means read the database.
- **Sign-in allow-list = the people table.** `HELIX_ALLOWED_USERS`, if
  set, still overrides it exactly (Render does not set it).
- **Invites work like GitHub collaborator invites**: pending until the
  invited GitHub login signs in, expire after 7 days, can be resent or
  cancelled, have a copyable link. A pending invitee cannot approve or
  review anything yet.

### Roles (Discord-style hierarchy)

| Role | Can do |
|---|---|
| **Owner** (co-owners, any number) | Everything: services, connections, the whole team, including making and removing admins and other co-owners |
| **Main owner** (exactly one, VrajVed) | Same as owner; cannot be removed or demoted by anyone; can hand main ownership to another co-owner |
| **Admin** | Services, connections, and inviting, renaming or removing **members** |
| **Member** | Sees everything; decides the approvals and reviews `state/deciders.py` allows |

Nobody changes someone at their own rank or above, except owners acting on
other co-owners. An invite can only grant a role the inviter could manage.
High-risk actions stay behind approval whoever is signed in.

### Naming

- A service's people who approve its changes are **Approvers** (and a
  **default approver**) on every page and message, so "owner" only means
  the team role. Code, columns and the CODEOWNERS-style path rules keep
  their old names (`owners`, `default_owner`, `path_owners`).
- The person a change is routed to is shown as **"assigned to"**.
- People are shown by **display name**, with the GitHub login as a
  tooltip.

### Human-in-the-loop rules (unchanged from Phase 2)

| Decision | Who may make it |
|---|---|
| Approve or reject an action | An approver of the service, never the change's author |
| Approve or request changes on a PR | Only the reviewer Helix asked, never the author |

### Slack identity matching (three steps)

A person's Slack member id is found automatically, in order:

1. the email an admin types on the invite (Helix also DMs them the invite
   link when it finds them),
2. every verified email on their GitHub account, read at sign-in
   (sign-in asks GitHub for `user:email`),
3. a private, signed **"Link your Slack"** link Helix sends when an
   unlinked person clicks a Helix button in Slack; opening it while signed
   in binds the two accounts.

### Approve and Reject feel instant

The dashboard sends `?wait=false`: the decision is saved and answered with
a 202 at once, the button says "Approving...", the run resumes in the
background, and the page reloads when the incident moves on. The JSON API
without the flag still waits, as the CLI expects.

---

## 4. How the hosted app works

### Pages

| Path | What |
|---|---|
| `/` | Public landing page; signed-in visitors go to the dashboard |
| `/ui/` | Home: what needs you, open incidents, 30-day stats, activity, tool status |
| `/ui/incidents`, `/ui/incidents/{id}` | Incident list and detail (links to the Slack thread, Jira ticket, PR) |
| `/ui/approvals` | Approvals and reviews queue |
| `/ui/connections` | Slack, Jira, GitHub App and PR bot settings (admin and above) |
| `/ui/team` | People, roles, invites, Slack linking |
| `/ui/services` | Services, approvers, reviewers, path rules |
| `/invite/{token}` | Public invite page |
| `/ui/team/link-slack` | Slack self-link confirmation |
| `/healthz` | Liveness; public; answers GET and HEAD |

### Code map for the new parts

| Concern | Where |
|---|---|
| Sign-in, sessions, admin gate, `next` redirects | [helix/auth.py](helix/auth.py) |
| Team rules (roles, invites, main owner, Slack link) | [state/team.py](state/team.py), page in [helix/team_ui.py](helix/team_ui.py) |
| Services rules and page | [state/services.py](state/services.py), [helix/services_ui.py](helix/services_ui.py) |
| Registry read (file or database), owner and reviewer resolution | [agent/ops/registry.py](agent/ops/registry.py) |
| Who may decide | [state/deciders.py](state/deciders.py) |
| Slack lookups for people | [helix/slack_people.py](helix/slack_people.py), [integrations/slack/oauth.py](integrations/slack/oauth.py) |
| Connections and PR bot | [helix/connections_ui.py](helix/connections_ui.py), [state/connections.py](state/connections.py), [state/pr_bot.py](state/pr_bot.py) |
| Dashboard pages and the `person` filter | [helix/ui.py](helix/ui.py), templates in [ui/templates/](ui/templates/), design system in [ui/static/style.css](ui/static/style.css) |
| Decision endpoints (`?wait=false`) | [helix/api.py](helix/api.py) |

### Database

New tables since Phase 2: `connections` (0012-0014), `pr_bot_settings`
(0013), `people` and `services` (0015), `invitations` (0016, 0017), roles
and `people.main_owner` (0018, a partial unique index allows only one).
Migrations run at every boot.

### Deploying

- Render service `helix` (`srv-db7noi49v7es73cn286g`), Docker, free plan,
  **auto-deploy off**. Deploy from the fork's `main` with the Render CLI:
  `render deploys create srv-db7noi49v7es73cn286g --commit <full sha> --wait --confirm`
  (it needs the full commit id).
- Boot ([scripts/render-start.sh](scripts/render-start.sh)): fix the
  database URL, refuse to start without sign-in configured, run
  `alembic upgrade head`, run `helix team import` (a no-op once the team
  exists), then `helix serve`.
- Env vars are set in the Render dashboard; [render.yaml](render.yaml)
  lists their names. Secrets (OAuth client secrets, signing secret, the
  GitHub App key as a Secret File) are typed into Render only, never into
  chat, git or logs.
- **GitHub events:** the GitHub App's webhook delivers to
  `/webhooks/github`, signed with `HELIX_GITHUB_WEBHOOK_SECRET` (set once
  for the product, in Render and in the App's webhook settings; an unset
  secret closes the route). Pull requests on a registered repository start
  the change review; failed check runs are only recorded until Helix reads
  live GitHub evidence (Render diagnoses from recorded evidence).
- **Free tier limits:** the service sleeps after 15 idle minutes;
  **UptimeRobot** pings `/` and `/healthz` every 5 minutes to keep it awake
  and email on downtime. 750 instance hours a month covers one
  always-on service. **The free Postgres expires 2026-11-13.**

---

## 5. How we work (keep these)

- **Git:** commit straight to `main` on the fork (`VrajVed/Helix`), push,
  open a PR into `nikhilpravinpise/Helix`, a human merges it, then
  fast-forward the fork (`git fetch upstream && git merge --ff-only
  upstream/main && git push origin main`). Never merge both locally and
  through the PR. Claude Code's auto mode refuses to merge PRs on its own,
  so merging is always a person's click.
- **No attribution anywhere:** no co-author lines, no "Generated with
  Claude Code", and **no em dashes** in code, docs, commits or PRs.
- **Tests are the source of truth.** A failing test means the code is
  wrong; a test changes only when the requirement changed, and the commit
  says so. Reproduce a bug before fixing it.
- **Run the real thing.** Every real defect in Helix was found by running
  code, not by the suite. For UI work, take screenshots at desktop and
  phone width and fix visual defects on the way.
- **Secrets:** never print `.env` values; never edit `.env` during a run;
  leave other projects' containers alone; never `--remove-orphans`.
- **/slice-plan** ([.claude/skills/slice-plan/SKILL.md](.claude/skills/slice-plan/SKILL.md)):
  how we plan work. Gather candidates, investigate, write slices with a
  proof that fails first, ask the open decisions, build one slice per
  commit, report. Every PR since #39 was built this way.
- Build work is done by Sonnet subagents, one slice each, and every result
  is re-checked before committing (lint, the full suite, migrations on real
  Postgres, screenshots). Subagent output has needed fixes every time.

---

## 6. Where the product is going (DX plan)

Full plan: [future_plans.md](future_plans.md). Summary:

**Done (Stage 1 for one team, and most of Stage 2):** connections by OAuth
and GitHub App, PR bot settings, landing page, dashboard home, tool links,
team and services in the dashboard, invites, roles, Slack matching,
instant approvals.

**Next, in order:**

1. **First-run setup guide** (issue #41): a team checklist on Home ticked
   off from real state (recommended), or per-person accounts first.
2. **Any team can use Helix** (issue #38):
   - one app per tool, owned by the product, with public distribution
     turned on (Slack app, GitHub App "any account", Atlassian sharing);
   - sign-up instead of an allow-list: anyone signs in with GitHub and
     creates or joins a team;
   - connections, PR bot settings, incidents, outbox rows and the
     registry scoped per team (Stage 3, multi-tenancy).
3. **Live updates without full-page reloads** (the dashboard redesign
   item in future_plans.md).
4. **Running it as a service** (Stage 4): paid database, backups,
   monitoring, a real domain.

**Open product questions** (future_plans.md "Open questions"): hosted only
or also self-hosted; is the sandbox cluster part of the product or does
Helix connect to a team's own clusters (needs a stronger safety story for
production writes); which tools are first-class (Linear and Trello are
optional).

---

## 7. Open GitHub issues on nikhilpravinpise/Helix

| # | Title | Type | Recommended answer / next step |
|---|---|---|---|
| [#38](https://github.com/nikhilpravinpise/Helix/issues/38) | Product DX: let any team connect Slack, Jira and GitHub | Plan | Retire the dev Slack app from the product, turn on distribution for all three tools, then build teams (Stage 2 and 3) |
| [#41](https://github.com/nikhilpravinpise/Helix/issues/41) | First-run setup guide | **Decision** | Team checklist on Home |
| [#42](https://github.com/nikhilpravinpise/Helix/issues/42) | Clear the old demo incidents from Render | **Decision** (deletes data) | Export with `helix export-history`, then delete them |
| [#43](https://github.com/nikhilpravinpise/Helix/issues/43) | Delete `config/service_registry.yaml` | Task | After someone signs in and confirms the import; keep `helix team import --path` |
| [#44](https://github.com/nikhilpravinpise/Helix/issues/44) | Slack user scopes and a real Slack check | Setup + check | Add `users:read` and `users:read.email` to the Slack app, reconnect Slack, then try invite, Find in Slack and the self-link for real |
| [#45](https://github.com/nikhilpravinpise/Helix/issues/45) | Small product follow-ups | **Decision** (yes/no each) | See the issue |

Open PR: [#47](https://github.com/nikhilpravinpise/Helix/pull/47), already
live on Render; merge it.

---

## 8. Known gaps and risks

- **Database expiry 2026-11-13** (free Render Postgres). Highest priority.
- **Slack matching is tested only against simulated Slack replies.** The
  real check is issue #44.
- **Single team only.** Connections are one per tool for the whole Helix,
  so a second team connecting would replace ours (issue #38).
- **Old demo incidents** still sit in the Render database and show in the
  stats (issue #42).
- **FOLLOWUPS still open:** I5 (a Notion page deleted in the UI reads as
  archived, not missing; not reproduced) and the #21 items now tracked in
  #42. (#16, GitHub webhooks, is done on the hosted Helix.)
- **GitHub CI failures are recorded, not diagnosed** on the hosted Helix
  until it reads live GitHub evidence (the next planned piece of work).
- **GCP deployment** for the course, deferred by decision.
- **Free tier speed:** the first request after a deploy is slow; the
  instance has little CPU, which is why approvals resume in the
  background.

---

## 9. Document map

| Read | When |
|---|---|
| [README.md](README.md), [CONTEXT.md](CONTEXT.md) | What Helix is, the architecture and the phase plan |
| [CLAUDE.md](CLAUDE.md) | Engineering rules for any agent working here |
| [plan.md](plan.md) | Phase-by-phase plan and status |
| [future_plans.md](future_plans.md) | The product plan and every product decision |
| [FOLLOWUPS.md](FOLLOWUPS.md) | Parked findings and their status |
| [HANDOFF-PHASE2-EXIT.md](HANDOFF-PHASE2-EXIT.md) | Phase 2 results and the live local setup |
| [docs/verifying.md](docs/verifying.md), [docs/bring-up.md](docs/bring-up.md) | Running tests and bringing the stack up locally |
| [docs/benchmark/phase-2-evaluation.md](docs/benchmark/phase-2-evaluation.md), [docs/benchmark/tolerance.md](docs/benchmark/tolerance.md) | Benchmark results and tolerance bands |
| [docs/architecture/](docs/architecture/) | Agent graph, state machine, safety policy, provenance |
| [docs/audit-2026-10-05.md](docs/audit-2026-10-05.md) | The codebase audit and its fixes |
| [docs/runbooks/operator.md](docs/runbooks/operator.md), [DEMO.md](DEMO.md) | Operating Helix locally and the local live demo |
| [infra/gcp/README.md](infra/gcp/README.md) | The deferred GCP deployment |
