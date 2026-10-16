# Future plans: Helix as a usable product

**Status:** outside the current plan. `plan.md` is the project scope
(Phases 1, 1.5 and 2, plus OPT-1) and is not changed by anything here.
None of this starts before Phase 2 exits (2.7 and 2.11 measured), and
every stage below keeps the engineering rules of CONTEXT.md and CLAUDE.md.

Recorded 2026-10-04.

---

## The goal

Today Helix is configured by editing `.env` and `config/service_registry.yaml`
and runs on one laptop. As a product, a team should be able to:

1. Open Helix in a browser and sign in.
2. Click **Connect** for Slack, Jira, GitHub, Notion and Linear, approve
   each one in that tool's own OAuth screen, and be done - no tokens to
   copy, no files to edit.
3. Pick their Slack channel, Jira project and repositories from dropdowns,
   and say who owns which service.
4. See Helix start handling their incidents and pull requests.

## Where Helix is today vs. what a product needs

| Area | Today | Product |
|---|---|---|
| Configuration | `.env` + registry YAML | Settings UI, stored in Postgres |
| Credentials | Personal tokens pasted into `.env` | OAuth per tool, tokens encrypted at rest, refreshed automatically |
| GitHub identity | A person's token; PRs authored by that person | A GitHub App; PRs authored by `<app>[bot]` (FOLLOWUPS #15) |
| Who uses it | One person | Several users with roles |
| Tenancy | One team, one database | Many teams, isolated data |
| Hosting | A laptop, kind cluster | A hosted service with a public HTTPS URL |
| Events from tools | Polling GitHub, Slack Socket Mode | Webhooks over a public URL (FOLLOWUPS #16), polling kept as fallback |
| UI | Read-only dashboard | Dashboard + onboarding + connections + settings |

## Stages

Each stage is useful on its own and builds on the last.

### Stage 0 - Prerequisites

- **Phase 2 exited**: the measurements in plan.md 2.7 and 2.11 done.
- **Hosting**: a public HTTPS URL. OAuth providers will not redirect to
  `localhost` for anyone but the developer, and webhooks need somewhere to
  land. The GCP deployment the course already requires (D23) is the natural
  home; ngrok (FOLLOWUPS #16) covers development.
- **Secret handling**: an encryption key for stored tokens, held outside
  the database (cloud secret manager when hosted, env var locally).

### Stage 1 - Connections, for one team

The biggest UX win for the least risk: no accounts, no multi-tenancy, just
no more hand-edited credentials.

- **Connections page** in the dashboard with a card per tool: status
  (connected / expired / not connected), the account it is connected as,
  **Connect**, **Disconnect** and **Test**.
- **OAuth flows**, each with its own quirks:
  - **Slack**: OAuth v2 install of the Helix app with `chat:write`; the
    install returns the bot token. Interactivity via the public URL (or
    keep Socket Mode, which also needs the app-level token).
  - **Jira Cloud**: OAuth 2.0 (3LO) with offline access; tokens expire and
    must be refreshed; requests go through the site's `cloudid`.
  - **GitHub**: install the GitHub App (FOLLOWUPS #15) on chosen repos;
    Helix mints short-lived installation tokens itself.
  - **Notion**: public integration OAuth; the user picks which pages Helix
    may write under during consent.
  - **Linear**: OAuth 2.0; pick the team after connecting.
- **Token store**: a `connections` table holding provider, account, scopes,
  encrypted access/refresh tokens and expiry. Providers read credentials
  from it instead of settings; `.env` stays as a fallback for development.
- **Pickers instead of IDs**: list Slack channels, Jira projects, Linear
  teams, Notion pages and repositories through the connected accounts.
- **People without YAML**: an editor for services, owners and reviewers,
  with Slack ids looked up from each person's email (`users.lookupByEmail`)
  rather than copied from profiles.

### Stage 2 - Accounts and roles

- **Sign in with GitHub** (or Google), so actions are tied to real users.
- **Roles**: admin (connections, settings), responder (approve, reject,
  review), viewer.
- Approvals and reviews made in the dashboard are recorded against the
  signed-in user, the same way Slack clicks are today.

### Stage 3 - Multi-tenancy

- An **organisation** model: every incident, event, outbox row, connection
  and registry entry belongs to one org, enforced on every query.
- Per-org workers (watchers, consumer) and per-org rate limits.
- Per-org LLM keys or budgets, so one team's usage cannot exhaust another's
  - the Groq daily cap already limits a single team.
- Kafka topics or partition keys scoped per org.

### Stage 4 - Running it as a service

- Webhooks as the primary event path, signature-verified per provider.
- Onboarding wizard: connect tools, add a first service, send a test
  incident, see it in Slack.
- Observability of Helix itself, backups, data retention and deletion.
- Pricing and billing only if it is ever offered to others.

## Dashboard redesign

Recorded 2026-10-05: Vraj finds the current dashboard (`ui/`, served by
`helix serve` at `/ui/`) hard to look at, and it is a blocker for showing
Helix to anyone. It is a set of server-rendered Jinja pages that were
built to expose state, not designed.

What a redesign should cover, before any of the stages above build on it:

- A real visual design: type scale, spacing, a restrained palette, light
  and dark themes, no raw JSON dumps (FOLLOWUPS: the incident page's
  file dump).
- An incident list that reads at a glance: status, service, severity,
  owner, age, and what Helix is waiting on.
- An incident page as a timeline: signal, evidence, hypothesis, plan,
  policy verdict, approval (who and when), execution, verification.
- Approvals and reviews as clear cards with the reason and the
  allowed deciders, matching the Slack cards.
- Live updates without reloading.

Keep it read-mostly and behind the same rules as everything else: a
decision made in the dashboard goes through `record_decision` and the
human-in-the-loop rules (state/deciders.py), and the dashboard is never
exposed on a public URL without sign-in (Stage 2).

## Product DX (2026-10-15)

Recorded 2026-10-15: Helix on Render is a product, not a demo, and the
focus is the experience of a team using it. Stage 1's connections
(Slack and Jira OAuth, the GitHub App, the PR bot) landed before this.

Done:

- No demo framing: no "Demo" badge, no boot seeding, production label;
  "Send a test incident" checks the connected tools end to end (d52eec8).
- A public landing page at `/`; signed-in visitors go straight to the
  dashboard (cff50bc).
- A dashboard home at `/ui/`: greeting, what needs you (real decider
  rules), open incidents, 30-day stats, activity, tool status; the
  incident list moved to `/ui/incidents` (19a93e4).
- Links out to the team's Slack channel, Jira project and GitHub
  repository, and per incident to its Slack thread, Jira ticket and pull
  request (95f381d).
- A Connections page in plain English, admin detail behind "For admins"
  (83847bc).
- A draggable, remembered sidebar width (7520312).

Open decision - **setup tutorial for new users.** Vraj wants a first-run
setup guide (connect Slack, pick a channel, connect Jira, pick a
project, install the GitHub App, pick the PR repository, send a test
incident, see it in Slack). There are no per-person accounts yet:
connections belong to the team and sign-in is an allow-list. Choose:

1. A **team checklist** on Home, ticked off from real state and shown
   until setup is complete (each person can hide it) - no new model; or
2. **Per-person accounts first** (Stage 2), so each new person gets their
   own first-run tour.

Still missing for a team to onboard without editing files (from Stage 1
and Stage 2 above, not yet built):

- An editor for services, owners and reviewers (today
  `config/service_registry.yaml`), with Slack ids looked up by email.
- Inviting people: sign-in is limited to the registry's `people` or
  HELIX_ALLOWED_USERS, so adding a teammate means a config change.
- Live updates without full-page reloads (the dashboard redesign item
  above).
- Smaller items are in FOLLOWUPS.md #21.

### Next: team and services in the dashboard (planned 2026-10-15)

Today `config/service_registry.yaml` is a file in the code that lists
each service with its owners, reviewers and path rules, and the people
(GitHub login, name, Slack id) - which is also the sign-in allow-list.
Adding a service or a teammate means editing it, committing, pushing
and redeploying. This plan moves all of it into the dashboard. To be
planned slice by slice with /slice-plan in a fresh session.

Decided by Vraj:

- **Roles: admins and members.** Admins manage services, the team and
  connections. Members see everything and decide the approvals and
  reviews state/deciders.py lets them decide. VrajVed and
  nikhilpravinpise start as admins. High-risk actions stay behind
  approval whoever is signed in.
- **The YAML is imported once.** On first start the file's services and
  people are imported into Postgres; after that the dashboard is the
  only editor. The benchmark keeps its own fixed
  `benchmark/service_registry.yaml`.

What to build:

1. **Services page.** Add, edit and remove services: team, repository,
   owners, default reviewers, dependencies and path rules (CODEOWNERS
   order, last match wins). Stored in Postgres. agent/ops/registry.py
   and state/deciders.py keep their rules and read from the database
   instead of the file.
2. **Team page.** Invite a teammate by GitHub login and choose admin or
   member; remove people. The people table becomes the sign-in
   allow-list (HELIX_ALLOWED_USERS stays as an override). Slack ids are
   matched through the connected Slack (`users.lookupByEmail`) instead
   of being copied from profiles.
3. **Approve and Reject show progress.** Today the decision resumes the
   run inside the request, so on Render's free CPU the button greys out
   for ~40s. The button should say "Approving..." at once, the resume
   should run in the background, and the page should update itself
   when it finishes.
4. **One-time import** of `config/service_registry.yaml`, safe to run
   twice, with a test that every service and person arrives intact.

Decided in the planning session (2026-10-15):

- **Invites work like GitHub collaborator invites.** An invite is
  pending, shows who sent it and when, and expires after 7 days; admins
  can cancel or resend it and copy its link, and Helix DMs the person in
  Slack when it finds them. Signing in with the invited GitHub login
  accepts it. Until then the person cannot own or review a service.
- **The last admin cannot be removed or demoted**; the page says why.
- **The import runs only into an empty team.** If the database already
  has people or services it does nothing and logs why, so dashboard
  edits always win and running it on every boot is harmless.
- **Slack ids are matched in three steps:** the email the admin typed
  on the invite, then every verified email on the person's GitHub
  account (sign-in asks for `user:email`), then a self-link: an unlinked
  person clicking a Helix button in Slack gets a private, signed "Link
  your Slack to Helix" link that binds the two when opened while signed
  in. Slack needs `users:read` and `users:read.email`, so it is
  reconnected once.
- **Where the registry is read from:** HELIX_SERVICE_REGISTRY set means
  that file (benchmark, tests); unset means the database.

Slices: 1 tables and import, 2 readers switch to the database, 3 Team
page, 4 Services page, 5 background Approve/Reject, 6 display names vs
logins (FOLLOWUPS #21), 7 delete config/service_registry.yaml once the
import is confirmed on Render. Slices 1-6 merged in #40.

### Roles: owners, admins, members (decided 2026-10-15)

Vraj asked for a Discord-style hierarchy. Decided:

- **Three roles: Owner, Admin, Member.** Several people can be Owners
  (co-owners) and share Owner power. One of them is the **main owner**,
  who cannot be removed or demoted by anyone and can hand main ownership
  to another co-owner. VrajVed is the main owner; today's admins
  (VrajVed, nikhilpravinpise) become co-owners.
- **Owners** manage everything: services, connections and the whole
  team, including making and removing Admins and other co-owners
  (never the main owner).
- **Admins** manage services and connections, and invite, rename or
  remove Members. They cannot change Admins or Owners.
- **Members** decide the approvals and reviews state/deciders.py lets
  them decide, as before.
- Nobody acts on someone at their own rank or above, except Owners on
  other co-owners. Invites can grant a role only up to what the inviter
  could later manage.
- **Service "owners" are called Approvers** on the pages and in
  messages, so "owner" only means the team role. The registry fields and
  CODEOWNERS-style path rules keep their names in code.

Slices: A roles and the rank rules, B the Team page for them, C the
Approvers rename.

## Principles that carry over unchanged

- **Postgres is the source of truth**; Slack, Jira, Notion and the graph
  stay projections fed by the outbox.
- **Least privilege**: request only the OAuth scopes each feature uses,
  show users exactly what is requested, and let them disconnect.
- **High-risk actions stay behind approval**, whoever is signed in.
- **No arbitrary shell for the LLM**, and vendor APIs stay behind
  capability interfaces - the OAuth work changes how credentials arrive,
  not how agents reach tools.
- Secrets never in git, logs or prompts; stored tokens encrypted.

## Open questions

- Hosted only, or also self-hosted (a Docker image a team runs itself)?
  Self-hosting avoids storing other people's tokens, but every team then
  registers its own OAuth apps.
- Is the sandbox cluster part of the product, or does Helix only connect to
  a team's own clusters? The latter needs a much stronger safety story for
  production writes.
- Which tools are first-class? Linear and Trello are optional in plan.md.

## Related

- FOLLOWUPS #15 - GitHub App (Stage 1's GitHub connection)
- FOLLOWUPS #16 - ngrok tunnel and webhooks (development stand-in for
  Stage 0 hosting)
- plan.md D23 - GCP deployment (Stage 0 hosting)

### Self-approval and reviewer reassignment (decided 2026-10-16)

Vraj changed the Phase 2 human-in-the-loop rule:

- **Owners and admins may approve their own change, guarded.** The
  automatic pick still sends the review or approval to someone else
  first. Self-approving is a separate, explicit "Approve my own change"
  action on the dashboard, offered only to owners and admins (never
  members, never from a Slack click), and every such decision is shown
  as **self-approved** on the dashboard, in Slack and in the record.
- **Owners and admins can reassign a pending review** to anyone on the
  team except the change's author. The automatic pick from the service's
  reviewer list stays the default.
- Members keep the old rule: nobody else's role changes, and the
  benchmark (file registry, no roles) keeps the strict rule.
