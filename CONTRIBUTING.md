# Contributing

Thanks for helping. This guide covers the dev loop, the code layout and, in detail, how to add a provider.

## Dev loop

```bash
uv sync
uv run pytest            # must be green before every commit
uvx ruff check jarvis runner tests --select E9,F --ignore F811
```

Python 3.12, one runtime dependency (`pyyaml`): keep it that way unless there is a strong reason. The server is a stdlib HTTP server over SQLite; tests start it on a random port and talk to it with `urllib`.

## Layout

| Path | What |
|---|---|
| `jarvis/` | the server: planner, briefing, commands, runs (queue and fallback), config, SQLite access |
| `jarvis/providers/` | one adapter per model CLI, behind the `Provider` interface |
| `runner/` | the process that executes runs on a machine with your repos |
| `prompts/`, `locales/` | model prompts and user-facing strings, per locale |
| `db/` | `schema.sql` (version 1) plus numbered migrations applied on connect |
| `n8n/workflows/` | the ingest and briefing workflows |
| `config/projects.example.yaml` | the documented configuration |

Server and runner are deployed separately and share `jarvis/`. Keep the HTTP contract between them backward compatible (add fields, do not rename), and add a migration for every schema change (`db/migrations/NNNN_name.sql`; SQLite cannot alter a CHECK, rebuild the table).

## Adding a provider

A provider is a class in `jarvis/providers/<name>.py` that builds the command lines for one CLI and reads its output. The runner and the planner never branch on the CLI name: they look the provider up in the registry by the `cli:` field of an executor.

Walkthrough with **Codex** (`codex`) as the example. The flags below are a sketch from the Codex docs; verify each against the version you have installed (`codex exec --help`) before relying on it, and record what you verified.

### 1. Learn the headless contract of the CLI

Write down, with real runs, the facts you need (see `docs/oss-providers.md` for the table of the existing providers):

- non-interactive invocation (`codex exec "<prompt>"`), working directory, model flag (`-m`)
- output format and where the final answer lives (`--json` emits JSONL events)
- how to auto-approve edits, and how to forbid them (sandbox modes: `read-only`, `workspace-write`)
- native structured output (`--output-schema <file>`), or none
- what "not logged in", "rate limited" and "model not available" look like (exit code and text)
- a quota/usage command, if any

Save real outputs as fixtures under `tests/fixtures/providers/<name>/`. Heuristics you could not verify must say so in a comment.

### 2. Implement the interface

```python
# jarvis/providers/codex.py
from jarvis.providers.base import QUOTA, Provider, ProviderError, last_result, tail


class CodexProvider(Provider):
    name = "codex"              # the `cli:` value in projects.yaml
    binary = "codex"            # looked up in PATH by check_ready()
    # Declare only what you verified. "execute" and "review" are the minimum for an executor/reviewer;
    # "plan_structured" means the CLI enforces a JSON schema natively; "quota" means usage() works.
    capabilities = frozenset({"execute", "review"})

    def build_exec_cmd(self, executor, prompt, *, commands, skip_permissions, timeout_minutes):
        return ["codex", "exec", "--json", "-m", executor["model"], "--sandbox", "workspace-write", prompt]

    def build_plan_cmd(self, executor, prompt, *, timeout_minutes):
        return ["codex", "exec", "--json", "-m", executor["model"], "--sandbox", "read-only", prompt]

    def build_review_cmd(self, reviewer, prompt, *, schema, timeout_minutes):
        ...  # read-only; put the schema in the prompt if the CLI has no schema flag

    def build_structured_cmd(self, model, system, message, schema):
        ...  # no tools; for providers without "plan_structured" the schema is already in `message`

    def parse_structured(self, returncode, stdout, stderr):
        ...  # return the JSON object or raise ProviderError

    def parse_output(self, code, output):
        # -> (summary, blocking error or None, quota_exhausted)
        ...

    def parse_review(self, code, output):
        # -> (review dict {"verdict", "summary", "issues": [...]} or None, error or None, quota_exhausted)
        ...

    def allowed_commands(self, commands, submodule_paths):
        ...  # shell commands the agent may run; shown in the task prompt
```

Rules of thumb, taken from the existing adapters (`claude.py`, `agy.py`, `copilot.py`):

- Command builders are pure functions returning an argv list; no I/O, easy to unit-test.
- Exit code `124` is the runner's timeout; handle it in `parse_output`/`parse_review`.
- Return `quota_exhausted=True` when the CLI says you ran out of quota (use the shared `QUOTA` regex plus provider-specific texts). The server then falls back to the next executor.
- Never let an agent push or call `gh`; the runner does that. Deny those commands in the CLI's permission syntax if it has one.
- Optional hooks: `event_lines()` turns streaming events into log lines for `log`/`follow`; `usage()` and `exhausted()` implement the `quota` capability; `quota_reset()` parses a reset time out of a limit message; `uses_task_suffix` is for CLIs that support an advisor sub-agent.

### 3. Register it

In `jarvis/providers/__init__.py`:

```python
from jarvis.providers.codex import CodexProvider
register(CodexProvider())
```

### 4. Test it

Add `tests/test_codex_provider.py` covering: exact argv for exec, plan and review; `parse_output`/`parse_review` on your fixtures (success, timeout, not logged in, rate limited); `parse_structured`; `check_ready()` with `shutil.which` patched. Use `tests/test_providers.py` as the pattern. `uv run pytest` must stay green.

### 5. Use it

```yaml
executors:
  codex-default: { cli: codex, model: gpt-5-codex, family: openai }
fallbacks:
  openai: [claude-sonnet]
```

Then run a real task on a scratch repo from Telegram (`go N`) and describe what you saw in the PR. Mention anything you could not verify.

## Adding a locale

User-facing strings live in `locales/<code>/` (messages, command vocabulary, prompts). Copy `locales/en/`, translate, and add the code to the list of accepted `owner.locale` values; the tests check that every locale has the same keys and placeholders as `en`.

## Pull requests

- One topic per PR, with tests. Explain behavior changes and anything that affects deployment (new env var, migration, deploy order of server and runner).
- Do not commit secrets, real chat ids, hostnames or personal paths; use placeholders.
- Commit messages: short imperative subject, a body when the why is not obvious.

## Good first issues

- A Codex adapter (this guide)
- A mode without n8n (ingest and the briefing schedule inside the server)
- More locales
