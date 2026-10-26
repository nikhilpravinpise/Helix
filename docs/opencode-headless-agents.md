# Running Multiple Agents Headlessly on the Free OpenCode Tier

Notes from the OpenCode V2 docs (checked 2026-10-17) on running non-interactive
("headless") agents in parallel using the free models on the OpenCode provider.

## Short answer

Yes. `opencode run` is OpenCode's non-interactive mode, built for scripts and CI.
Each invocation creates its own session, so you can launch as many in parallel as
you want. The free models cost $0, so parallel runs cost nothing — subject to the
caveats below.

## Headless mode: `opencode run`

```bash
opencode run "Explain this repository"
```

Meant to be driven from scripts, CI jobs, and anything that wants model output
directly in the terminal.

### Flags (verified against the installed build)

| Flag | Purpose |
| --- | --- |
| `--agent` | Agent to use (built-in or custom) |
| `--model`, `-m` | `provider/model#variant`, e.g. `opencode/deepseek-v4.1-flash-free` |
| `--format json` | Machine-readable output (`default` or `json`) |
| `--session`, `-s` | Continue a session ID, or create it if missing |
| `--continue`, `-c` | Continue the last session |
| `--fork` | Fork the session before continuing |
| `--title` | Session title |
| `--file`, `-f` | File to attach to the message |
| `--thinking` | Show thinking blocks |
| `--auto` | Auto-approve permissions that are not explicitly denied |
| `--standalone` | Run with a private server instead of the shared background service |
| `--server` | Connect to a server URL instead of the background service |

## Parallel runs

Each `opencode run` is an independent session. Launch them concurrently:

```bash
# bash
opencode run --auto --model opencode/deepseek-v4.1-flash-free --agent reviewer "Review src/auth" &
opencode run --auto --model opencode/glm-5.3-flash-free --agent explore "Map the API routes" &
wait
```

```powershell
# PowerShell
Start-Job { opencode run --auto --model opencode/deepseek-v4.1-flash-free "Review src/auth" }
Start-Job { opencode run --auto --model opencode/glm-5.3-flash-free "Map the API routes" }
Get-Job | Wait-Job | Receive-Job
```

### Fan-out inside one run

A session's agent can also spawn subagents in child sessions (built-in
`general` / `explore`, or custom agents with `mode: subagent`). The parent
agent's `subagent` permissions control which agents it may launch. This is
orchestrated fan-out within one session rather than N separate CLI runs.

## Agents

- Built-ins: `build` (default), `plan` (read-only exploration), `general` and
  `explore` (subagents).
- Custom agents: `.opencode/agents/<name>.md` in the project or
  `~/.config/opencode/agents/<name>.md` globally. Frontmatter supports
  `description`, `mode`, `model`, `permissions`, `steps`, etc.
- Pick one per run with `--agent <id>`; set a default with `default_agent` in
  `opencode.json(c)`.

## Free models (OpenCode provider)

Selectable free IDs on the OpenCode gateway (reference as `opencode/<id>`):

- `opencode/deepseek-v4.1-flash-free`
- `opencode/glm-5.3-flash-free`
- `opencode/big-pickle`
- `opencode/space-bunny-free`
- `opencode/beam-free`
- `opencode/mimo-v2.6-flash-free`
- `opencode/longcat-2.5-preview-free`
- `opencode/nemotron-3-ultra-free`
- `opencode/nemotron-3.5-lightning-free`
- `opencode/nemotron-3.5-super-vl-free`

They are free on the OpenCode gateway "for a limited time" and can change or
disappear without notice.

## Caveats

- **Permissions.** Headless runs have nobody to answer an `ask`. An approval
  decision that resolves to `ask` needs a client decision; non-interactive
  clients must decide how to handle it themselves. Options:
  - pass `--auto` (auto-approves anything not explicitly denied), or
  - configure explicit `allow` / `deny` permission rules in `opencode.jsonc`
    so nothing ever prompts.
  `deny` rules always remain enforced.
- **No documented concurrency quota — and no guarantees.** The docs do not
  publish a rate limit for the free models, but they share gateway capacity.
  A handful of parallel agents is fine; large batches may throttle/queue.
  Don't build a CI farm on the free tier.
- **Ephemeral models.** Free models are limited-time offers. Keep a paid
  provider or a local runtime (Ollama / LM Studio / vLLM) as a fallback for
  anything that must keep working.
- **Privacy differs per free model.**
  - Zero-retention, not used for training: DeepSeek V4.1 Flash Free,
    GLM-5.3-Flash Free, LongCat 2.5 Preview Free, Space Bunny Free.
  - May be used to improve the model: Big Pickle, LL Alpha, Exo,
    MiMo-V2.6-Flash / MiMo-V2.5, Nemotron free endpoints (NVIDIA trial terms —
    do not submit confidential data).
  - Beam Free: not used for training, retained 30 days for safety; regional
    restrictions apply.
  Choose accordingly for sensitive code.
- **Server topology.** By default all CLI runs connect to one shared background
  service, which owns sessions, permissions, and tool execution — fine for
  parallel sessions. Use `--standalone` for a private per-run server (useful
  for full isolation) or `--server URL` to target a specific one.

## When to use the API/SDK instead

For heavier multi-agent orchestration, the HTTP API and `@opencode/client`
(TypeScript) are the more robust route — they create and drive sessions
programmatically and stream results. See:

- <https://opencode.ai/v2/docs/api>
- <https://opencode.ai/v2/docs/build/client>
- <https://opencode.ai/v2/docs/build/sdk>

## Sources

- CLI / automation: <https://opencode.ai/v2/docs/cli>
- Agents / subagents: <https://opencode.ai/v2/docs/agents>
- Models: <https://opencode.ai/v2/docs/models>
- Permissions: <https://opencode.ai/v2/docs/permissions>
- Free models & pricing: <https://opencode.ai/v2/docs/console/models>
- Providers: <https://opencode.ai/v2/docs/providers>
