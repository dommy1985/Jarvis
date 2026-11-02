# Changelog

All notable changes to Jarvis. Versions follow [Semantic Versioning](https://semver.org/); every `v*` tag publishes the Docker image `ghcr.io/dommy1985/jarvis:<version>` (linux/amd64 + linux/arm64).

Upgrade order when a release touches the server–runner contract: server first, then the runner. Migrations under `db/migrations/` apply automatically when the server connects to the database.

## [Unreleased]

## [0.1.8] - 2026-11-02

### Added
- **Parallel runs** (#24, #27). `runner.max_parallel` (default 1) lets the runner execute several unrelated runs at once. The server starts the oldest queued run that does not conflict with a running one: same project or submodule family, a project with `parallel_safe: false`, the same CLI when the provider is not `concurrent` (Copilot), the same CLI when it was rate limited in the last minutes, or the same parent PR. `queue` on Telegram shows why a run waits.
- **Planner dependencies and stacked PRs** (#25, #28). The planner may mark that an item needs the code of an earlier one (`depends_on`, shown as "⛓ after N"). The dependent run waits while the other is queued or running, then starts on its branch (`agent/<id>`) with a draft PR stacked on it, or waits for the merge if that PR touches a submodule. A failed or never-started dependency does not block and the owner is notified. When the PR below merges, Jarvis proposes `repair N`, which rebases the stacked PR onto the normal base and retargets it.
- **`go 1 3 together`** (#25). Groups runs that ignore the dependency and same-project rules; `parallel_safe: false` and non-concurrent CLIs still apply.
- **Mailbox between active runs** (#26, #29). With `max_parallel > 1` each executor gets `jarvis-msg` (`peers`, `send`, `ask`, `reply`, `inbox`) to ask another running agent about its task. A local proxy on 127.0.0.1 with a per-run token forces the sender and strips secrets; messages are stored in `run_messages`, written to both runs' logs, limited in size and count, and always framed as untrusted data. Claude receives unread messages after each tool through a PostToolUse hook; other CLIs poll as the prompt says.

### Changed
- The runner sends the runs it is still executing with each claim, so only missing runs are treated as orphans. An old runner keeps one run at a time, and so does a new runner with an old server.
- Runner internals: one thread per run, locks on the API, pending file, SSH tunnel and per-repo git state, git auto-gc off in worktrees. SIGTERM stops running jobs as failed (worktree kept); the launchd/systemd stop timeout is 180 s.
- `stop`, `log` and `follow` without a number ask which run when more than one is active.
- Locale is per context (`i18n.using()`), safe across threads.

### Migrations
- `0015_parallel.sql`, `0016_run_messages.sql`.

### Known limits
- Inbox messages are marked read on the server: a proxy call that times out can lose them.
- A dependency whose plan is still only proposed blocks the dependent run.
- A stacked PR whose base PR is closed without merging is not flagged.

## [0.1.7] - 2026-10-30

### Added
- **`N skip` replaces the item** with a reserve the planner proposed (up to 5) and snoozes the task for `planner.skip_snooze_days`. After `planner.skip_ask_after` skips Jarvis suggests `N never` (or closing the GitHub issue).
- **`N never`** drops a task from planning until it is added by hand.

### Fixed
- Ingest: the last open task of a complete source was never closed when the snapshot had no tasks (`NOT IN (NULL)`).
- Runner: an unreadable line in `pending-updates.jsonl` no longer blocks delivery; it moves to `.bad` and the file is rewritten atomically.

### Migrations
- `0014_skip_memory.sql`.

## [0.1.6] - 2026-10-29

### Added
- Queuing a run with `go` assigns the GitHub issue to the owner (only on repos under `commands.write_owners`).
- `close_issue` GitHub command, and a prompt to close the issue after the run's PR merges.

### Fixed
- Unused import in `runs.py` (ruff F401) that failed CI.

## [0.1.5] - 2026-10-22

### Fixed
- A run left `running` after a runner restart blocked the queue for `stale_hours` with no message. It now fails at the next claim, the item goes back to approved, and Telegram tells where it stopped and offers `go N`, `go N note:` and `go N plan`. The runner claims only after delivering every pending outcome.

## [0.1.4] - 2026-10-22

### Added
- Agent questions to the owner (#8). Plan mode highlights the plan's open questions on Telegram and suggests `revise N <answer>`; an execution run that ends without changes because it needs clarification sends a Telegram notice that the agent made no changes and asks for clarification.
- `go N note: <text>` passes instructions to the agent; the note follows `approve`, `revise` and quota retries.

### Migrations
- `0013_owner_note.sql`.

## [0.1.3] - 2026-10-22

### Fixed
- Antigravity (`agy`): waiting and command notification lines no longer end up in PR descriptions and repair comments (#7).
- `install-runner.sh`: waits for the LaunchAgent to unload and retries the bootstrap ("Bootstrap failed: 5"), with a hint when it still fails.

## [0.1.2] - 2026-10-21

### Changed
- Plans on Telegram are readable: links to local files and absolute worktree paths are stripped, headings and bold are rendered, and the plan prompt asks for short single-line points.

### Docs
- Article about the project and animated architecture diagram in the README.

## [0.1.1] - 2026-10-20

### Changed
- Docker image built for linux/amd64 and linux/arm64 (v0.1.0 was amd64 only).

## [0.1.0] - 2026-10-20

First public release: a self-hosted assistant that collects your tasks (GitHub, optionally Jira), proposes each morning what to do and with which AI coding CLI, and runs the approved tasks in git worktrees ending in draft PRs, driven from a Telegram topic.

- Providers for Claude Code, Antigravity (`agy`) and GitHub Copilot CLI behind one interface, with ordered fallbacks on quota or missing CLI.
- Planner and GitHub commands work with any provider.
- English by default, Italian built in (`owner.locale`).
- One machine or split mode (server on a VPS, runner on a laptop over an SSH tunnel); macOS LaunchAgent and Linux systemd unit for the runner.
- Cross-review by a different model family before the draft PR, optional read-only plan first.

[Unreleased]: https://github.com/dommy1985/Jarvis/compare/v0.1.8...HEAD
[0.1.8]: https://github.com/dommy1985/Jarvis/compare/v0.1.7...v0.1.8
[0.1.7]: https://github.com/dommy1985/Jarvis/compare/v0.1.6...v0.1.7
[0.1.6]: https://github.com/dommy1985/Jarvis/compare/v0.1.5...v0.1.6
[0.1.5]: https://github.com/dommy1985/Jarvis/compare/v0.1.4...v0.1.5
[0.1.4]: https://github.com/dommy1985/Jarvis/compare/v0.1.3...v0.1.4
[0.1.3]: https://github.com/dommy1985/Jarvis/compare/v0.1.2...v0.1.3
[0.1.2]: https://github.com/dommy1985/Jarvis/compare/v0.1.1...v0.1.2
[0.1.1]: https://github.com/dommy1985/Jarvis/compare/v0.1.0...v0.1.1
[0.1.0]: https://github.com/dommy1985/Jarvis/releases/tag/v0.1.0
