# Jarvis

Jarvis is a small self-hosted assistant for developers who juggle many tasks and several AI coding CLIs.

Every morning it collects your open tasks (GitHub issues, optionally Jira), asks a model to propose **what to do today and who should do it**, and posts the briefing in a Telegram topic. You answer in plain commands (`ok`, `2 haiku`, `3 skip`, `hours 4`). Approved tasks are queued for a **runner** on your machine that creates a git worktree, runs the chosen coding agent (Claude Code, Antigravity, GitHub Copilot CLI), builds and tests, gets a cross-review from a different model family, and opens a **draft PR**. Nothing is pushed to your main branch and nothing runs without your approval.

- **Provider-agnostic.** Each CLI sits behind a small adapter (`jarvis/providers/`). Ordered fallbacks move a run to the next provider when one is out of quota or not installed. Adding a provider is one file (see [CONTRIBUTING](CONTRIBUTING.md)).
- **Uses the CLIs' own subscriptions.** No API keys are collected or proxied; Jarvis only starts the official CLIs you already logged into.
- **Plan first, if you want.** `go 4 plan` makes the agent write a read-only plan you approve, revise or discard before any code changes.
- **Small.** Python 3.12, SQLite, one dependency (`pyyaml`), a stdlib HTTP server. Ingest and the morning schedule run in n8n.

## How it fits together

```
 GitHub / Jira ──► n8n ──► Jarvis server ──► Telegram topic (briefing, commands, run status)
                              ▲  │
                   /runner/*  │  └─ SQLite
                              │
                           Runner (your laptop or the same host)
                              └─ git worktree ─► claude | agy | copilot ─► build/test ─► review ─► draft PR
```

Details in [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

## Requirements

- Docker with Compose (server and n8n)
- A Telegram bot (via @BotFather) and a group with topics enabled
- On the machine that runs the runner: Python 3.12 and [uv](https://docs.astral.sh/uv/), `git`, `gh` (logged in), and at least one provider CLI logged in: `claude`, `agy` or `copilot`
- A GitHub token for ingest (and for the Telegram GitHub commands, if you enable them)

## Quickstart: one machine

Server, n8n and runner on the same machine (your laptop, a home server).

```bash
git clone https://github.com/dommy1985/Jarvis.git jarvis && cd jarvis
cp .env.example .env                              # fill in the tokens, see the comments in the file
cp config/projects.example.yaml config/projects.yaml   # your name, timezone, projects, providers
docker compose --profile n8n up -d --build
curl -s http://127.0.0.1:8090/health              # the server answers
```

1. **Telegram.** Create a bot, add it to a group with topics, create a topic for Jarvis. Put the bot token, the group id, the topic id and your own Telegram user id in `.env` (`TELEGRAM_JARVIS_*`). Jarvis answers only that user, in that topic. Restart with `docker compose up -d`.
2. **n8n.** Open http://localhost:5678, create the owner account, then import `n8n/workflows/jarvis-ingest.json` and `n8n/workflows/jarvis-briefing.json` through the UI (Workflows → Import from file; the CLI import does not regenerate webhook ids). Replace the `REPLACE_WITH_*` placeholders (Jira host, Telegram group id), link the credentials (GitHub, Jira, Telegram, and an HTTP header credential carrying `JARVIS_API_TOKEN` as a Bearer token) and activate both workflows. Telegram replies reach Jarvis through the `Telegram Trigger` node, which needs a public `WEBHOOK_URL` (a tunnel such as Cloudflare Tunnel or ngrok works for a laptop).
3. **Runner.** On the same machine:

   ```bash
   JARVIS_RUNNER_TOKEN=<the value from .env> scripts/install-runner.sh
   ```

   This writes `~/.config/jarvis/runner.env` and installs a LaunchAgent (macOS) or a systemd user unit (Linux). The runner talks to `http://127.0.0.1:8090` by default. Check it with `launchctl print gui/$(id -u)/dev.jarvis.runner` or `systemctl --user status jarvis-runner`.
4. **First briefing.** In the Jarvis topic write `briefing`. Then `ok`, and `go 1` to queue the first task.

## Quickstart: split mode (server on a VPS, runner on your laptop)

The server and n8n run on the VPS with the same `docker compose` command. The server publishes its port only on `127.0.0.1`; the runner reaches it through an SSH tunnel it opens itself:

```bash
JARVIS_SSH_HOST=myvps JARVIS_SERVER_ENV=/path/to/jarvis/.env scripts/install-runner.sh
```

The installer copies `JARVIS_RUNNER_TOKEN` from the server's `.env` over SSH (it is never printed) and records `JARVIS_SSH_HOST` in `runner.env`. Without `JARVIS_SSH_HOST` the runner never opens a tunnel. Options and the systemd/LaunchAgent details are in [runner/README.md](runner/README.md).

## Configuration

`config/projects.yaml` (start from `config/projects.example.yaml`, every key is commented):

| Section | What it controls |
|---|---|
| `owner` | your name, timezone, locale (`en`, `it`), GitHub login |
| `planner`, `commands` | which executor writes the briefing / interprets GitHub commands (any registered provider) |
| `executors`, `routing` | named executors (`cli`, `model`, `family`) and which one gets which task type |
| `fallbacks` | ordered fallback per executor or family; also picks the cross-reviewer |
| `projects` | repo path, build/test commands, sources (GitHub repos, Jira projects), submodules, per-project overrides |

Project-specific coding rules stay in each repo (`AGENTS.md` / `CLAUDE.md`); Jarvis only needs paths and commands.

## Safety model

- Agents never push: the runner pushes only `agent/<id>` branches and opens draft PRs, and it refuses commits that contain files that look like secrets.
- Only tasks you created, or labelled `jarvis` (configurable), enter the queue, so a malicious ticket cannot enqueue itself.
- GitHub writes from Telegram require a preview and your `yes`, and only touch repos listed in `projects.yaml`.
- Jira descriptions, and the descriptions of projects with `redact_description: true`, never reach the planner model.
- Secrets live in `.env` and the runner's `runner.env`; the repo contains none.

## Development

```bash
uv sync
uv run pytest
```

See [CONTRIBUTING.md](CONTRIBUTING.md). MIT licensed.
