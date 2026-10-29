# Architecture

Jarvis proposes, you decide. No irreversible action happens without your approval.

## Components

**Server (`jarvis/`)** runs in Docker, next to n8n. A stdlib HTTP server over SQLite, no public port. It owns:

- the **planner**: one model call per morning that turns your open tasks into a briefing (what to do, with which executor, how long it needs of your attention). It sees only open, *eligible* tasks and returns JSON that is validated against a schema;
- the **briefing and command handling**: your Telegram replies (`ok`, `2 haiku`, `hours 4`, `go 3`, `approve 5`, ...) are parsed deterministically, with no model involved;
- the **GitHub commands**: free-text requests ("open an issue on my-app: new logo", "close issue on my-app: 12") are translated by a model into validated actions (create issue, add to project, set status, assign, close issue), previewed, and executed only after you confirm; new issues are assigned to the owner by default, and unassigned issues are automatically assigned when queued with "vai";
- the **queue and dispatcher** (deterministic): runs, fallback between executors, notifications, outcomes of the PRs opened by runs.

**n8n** does what it is good at: ingest from GitHub and Jira on a cron, the morning trigger, and the Telegram webhook that forwards your replies to the server. The workflows are in `n8n/workflows/` (credentials are not exported).

**Runner (`runner/`)** runs on a machine that has your repos and your logged-in CLIs. It pulls the queue from the server (`/runner/*` routes with their own token), and for each run: creates a git worktree on `agent/<id>`, copies local files you list, runs setup, starts the agent, builds and tests, collects the diff, asks a model from another family for a read-only review (one fix round for serious problems), pushes the branch and opens a **draft PR**. It reports progress, log lines and quota events back. The runner executes one run at a time and claims only when idle, so a claim that finds a run still `running` on the server means the runner restarted or crashed: the server fails that run at once, tells you on Telegram which step it stopped at and how to resume (`go N`, `go N note: ...`, `go N plan`). This assumes one runner instance per server, and the runner claims only after delivering every pending outcome.

**Providers (`jarvis/providers/`)** hide the differences between CLIs. A provider builds the command lines (execute, read-only plan, review, structured answer) and parses the output, including quota and login errors. The registry maps the `cli:` field of an executor to a provider. Server and runner share this code, so the runner's checkout must contain `jarvis/`.

## Flow

1. n8n ingests tasks; the server normalizes them into SQLite.
2. The planner writes the briefing; it is posted in the Telegram topic.
3. You confirm or change it. You can ask for a focused briefing once (`briefing on my-app`).
4. `go N` queues approved items. You can pass instructions or clarifications with `go N note: <instructions>`. For delicate tasks `go N plan` first runs a read-only planning run; any open questions are highlighted on Telegram so you can reply with `revise {id} <reply>`, then you approve, revise or discard the plan. If an agent stops without changes because it needs clarification, Jarvis sends a dedicated notification proposing `go {pos} note: <clarification>`.
5. The runner executes; `log` and `follow` show the agent's actions as it works.
6. Cross-review by the other model family, one fix round, build and tests again.
7. Draft PR (two linked PRs if the work touches a configured submodule).
8. The server watches the PR (merged, closed, review comments, red CI) and records the outcome. If a PR merges and the linked issue is still open, it asks on Telegram if you want to close it. If a PR gets review comments or fails CI it proposes `repair N`; it never starts on its own.

## Choosing an executor

Priority: your override on Telegram > project rule (`override`) > the planner's proposal > fallback. If the chosen executor is out of quota or its CLI is not installed on the runner machine, the run restarts with the first executor of its `fallbacks` list that was not tried and is not currently avoided. Providers with a usage command (`quota` capability) are checked before a run starts; for the others the server avoids a provider for the reset time reported in its last rate-limit message (default one hour). When the list is exhausted the run stays failed and you are told.

## Data

SQLite (`db/schema.sql` plus numbered migrations): tasks, briefings and items, runs, run log, quota events, GitHub commands, PR outcomes. Back it up by copying with SQLite's backup API (`scripts/backup-jarvis-db.sh` in the original deployment); the WAL files must not be copied by hand.

## Guardrails

- Agents never push; only the runner pushes `agent/<id>` branches and it blocks commits that include files that look like secrets.
- Only tasks you created or labelled (`eligible_label`) are eligible, which protects against prompt injection in ticket text. The ticket text reaches the agent delimited as data.
- Agents get shell access only for the project's own commands and read-only git, depending on the provider's permission model.
- GitHub writes from Telegram need a preview and your confirmation, are limited to `commands.write_owners` and to repos listed in the configuration, and there is no delete action.
- The Telegram handler accepts messages only from the configured user id, in the configured group and topic.
- Secrets live in `.env` and `runner.env`.

## Versioning of the server/runner contract

The runner and the server are deployed separately. New fields are added, not renamed; the runner supplies defaults for fields an older server does not send. When a release needs a migration, deploy the server first (migrations run on connect), then the runner.
