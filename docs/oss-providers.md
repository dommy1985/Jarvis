# AI CLI Providers Capability Matrix & Spike

This document details the non-interactive (headless) capabilities of the three supported CLI providers for Jarvis open-source extraction (GitHub Issue #5 / #7):
- **Claude Code** (`claude`)
- **Antigravity CLI** (`agy`)
- **GitHub Copilot CLI** (`copilot`)

All three run using the user's existing CLI subscription authentication (OAuth/browser login, device flow or stored tokens; no per-call API keys required).

---

## 1. Capability Matrix

| Feature / Capability | Claude Code (`claude`) | Antigravity (`agy`) | GitHub Copilot CLI (`copilot`) |
| :--- | :--- | :--- | :--- |
| **CLI Binary** | `claude` (Node/SEA binary) | `agy` (Go binary) | `copilot` (Node/Mach-O bundle) |
| **Headless / Prompt Flag** | `-p, --print <prompt>` | `-p, --print <prompt>` (or `--prompt`) | `-p, --prompt <prompt>` |
| **Output Formats** | `text`, `json`, `stream-json` | `text`, `json`, `stream-json` | `text`, `json` (JSONL/NDJSON) |
| **Structured Output / Schema** | Native: `--json-schema <schema>` | Native: `--json-schema <schema>` | Prompt-based schema (no `--json-schema` CLI flag) |
| **Final Answer Location (JSON)** | `data["structured_output"]` or `data["result"]` | `data["structured_output"]` or `data["response"]` | Extract from `assistant.message` event (`data["content"]`) or use `-s` in text mode |
| **Auto-Approve Edits** | `--permission-mode acceptEdits` or `--dangerously-skip-permissions` | `--dangerously-skip-permissions` or `--mode accept-edits` | `--allow-all` / `--yolo` or `--allow-all-tools` (env: `COPILOT_ALLOW_ALL=true`) |
| **Fine-grained Tool Restrictions** | `--tools <list>`, `--allowedTools`, `--disallowedTools` | `--sandbox` (terminal restrictions) | `--allow-tool`, `--deny-tool`, `--available-tools`, `--excluded-tools` |
| **Working Directory Flag** | Implicit (runs in `cwd`) or `--add-dir` | Implicit (runs in `cwd`) or `--add-dir` | `-C <dir>`, `--add-dir <dir>`, `--allow-all-paths` |
| **Timeout Flag** | CLI timeout or wrapper (`timeout` / subprocess timeout) | `--print-timeout <dur>` (e.g. `60s`, `15m`) | Subprocess timeout or `--max-ai-credits` |
| **Model Selection Flag** | `--model <model>` | `--model <model>` | `--model <model>` (or `--model auto`) |
| **Quota / Usage Command** | `claude -p /usage --output-format json` | `agy -p /usage --output-format json` | Non-interactive session stats (stderr in text mode; `usage` in json result event) |
| **Not-Installed Detection** | `shutil.which("claude") is None` | `shutil.which("agy") is None` | `shutil.which("copilot") is None` |
| **Not-Logged-In Detection** | Exit code != 0, stderr/JSON `is_error` (Heuristic, unverified by fixture) | Exit code != 0, `AGY_ERROR: {"error": ...}` | Verified fixture: Exit code 1, `Error: No authentication information found.` |
| **Rate-Limited Detection** | JSON `subtype: "rate_limit"` or text `quota\|limit\|credit` (Heuristic, unverified by fixture) | JSON/output `AGY_ERROR:` with quota/limit, or usage bucket `<= 0` | Exit code != 0, stderr/text `rate limit\|quota\|credits used` (Heuristic, unverified by fixture) |

---

## 2. Provider Breakdown

### 2.1 Claude Code (`claude`)

- **Version Tested**: `2.1.304`
- **Headless invocation**:
  ```bash
  claude -p "<prompt>" --output-format json --no-session-persistence
  ```
- **Output Formats**:
  - `text`: Plain text streamed to stdout.
  - `json`: Returns a single JSON object upon completion.
  - `stream-json`: Emits newline-delimited JSON events as execution proceeds.
- **Structured Output**:
  - Native `--json-schema '<json-schema-or-filepath>'`.
  - Parsed JSON result is provided directly in `data["structured_output"]` (with raw text in `data["result"]`).
  - Errors have `is_error: true` and `subtype: "<error_type>"`.
- **Permissions & Isolation**:
  - `--permission-mode acceptEdits` allows file edits while prompting for dangerous actions.
  - `--dangerously-skip-permissions` bypasses all permission prompts.
  - `--tools ""` disables all tools (used by `planner.py`).
  - `--tools "Read,Grep,Glob"` allows read-only operations for reviewers.
  - `--allowedTools` / `--disallowedTools` for fine-grained Bash/tool filters.
  - `--setting-sources ""` disables reading local project settings.
- **Model Selection**:
  - `--model <model>` (e.g. `claude-sonnet-4-6`, `claude-opus-4-6-thinking`).
- **Quota & Usage**:
  - Non-interactive command:
    ```bash
    claude -p "/usage" --output-format json --no-session-persistence
    ```
    Returns `local_command: "usage"` with human-readable percentages in `data["result"]`.
- **Error Detection**:
  - Exit code != 0 when command fails.
  - `data.get("is_error") is True`.
  - Rate limit / overload: `data.get("subtype") in ("rate_limit", "overloaded")` or substring match for `quota|limit|credit` (heuristic).

---

### 2.2 Antigravity CLI (`agy`)

- **Version Tested**: `1.4.3`
- **Headless invocation**:
  ```bash
  agy -p "<prompt>" --output-format json --print-timeout 60s
  ```
- **Output Formats**:
  - `text`: Plain text response to stdout.
  - `json`: Returns a single JSON object:
    ```json
    {
      "conversation_id": "eb37de3f-...",
      "status": "SUCCESS",
      "response": "PONG\n",
      "duration_seconds": 3.39,
      "num_turns": 1,
      "usage": { "input_tokens": 7671, "output_tokens": 2, ... }
    }
    ```
  - `stream-json`: Emits newline-delimited JSON messages (NDJSON) per turn.
- **Structured Output**:
  - Native `--json-schema '<json-schema-or-filepath>'`.
  - The parsed JSON object is populated directly in `data["structured_output"]`.
  - `data["response"]` contains the raw JSON string.
- **Permissions & Isolation**:
  - `--dangerously-skip-permissions`: Auto-approves all tool permission requests without prompting.
  - `--mode accept-edits`: Runs in edit-accepting mode without prompting for standard edits.
  - `--sandbox`: Enables terminal execution sandbox restrictions (used in `runner/runner.py:714` for reviewer commands to restrict destructive shell operations; read-only tools remain active).
- **Model Selection**:
  - Query models: `agy models`.
  - Available models: `gemini-3.8-flash-high`, `gemini-3.8-flash-medium`, `gemini-3.8-flash-low`, `claude-sonnet-4-6`, `claude-opus-4-6-thinking`, `gpt-oss-120b-medium`.
  - Specify model: `--model <model>`.
- **Quota & Usage**:
  - Non-interactive command:
    ```bash
    agy -p "/usage" --output-format json --print-timeout 60s
    ```
    Returns JSON containing `command.data.groups` with bucket IDs (`gemini-weekly`, `gemini-5h`, `3p-weekly`, `3p-5h`), `remaining_fraction` (0.0 to 1.0), and ISO `reset_time`. `/usage` does not consume quota.
- **Error Detection**:
  - Exit code != 0 on failure.
  - Output lines prefixed with `AGY_ERROR: <json>`.
  - Transient errors have `retryable: true` in the JSON payload.
  - Quota exhaustion: `remaining_fraction <= 0` in bucket or `AGY_ERROR` matching `QUOTA`.

---

### 2.3 GitHub Copilot CLI (`copilot`)

- **Version Tested**: `1.0.71`
- **Headless invocation**:
  ```bash
  copilot -p "<prompt>" -s --allow-all-tools
  ```
- **Output Formats**:
  - `text` (default):
    - Without `-s`: Answer is written to `stdout`; usage summary banner is written to `stderr`:
      ```text
      Changes    +0 -0
      AI Credits 0.03 (7s)
      Tokens     ↑ 13.2k (12.7k cached) • ↓ 6
      Resume     copilot --resume=<uuid>
      ```
    - With `-s, --silent`: `stderr` stats are suppressed; `stdout` contains strictly the final response text.
  - `json`: Emits newline-delimited JSON events (JSONL / NDJSON):
    - Session events: `session.mcp_servers_loaded`, `session.skills_loaded`, `session.auto_mode_resolved`.
    - Turn events: `user.message`, `assistant.turn_start`, `assistant.message_delta`.
    - Message event:
      ```json
      {"type":"assistant.message","data":{"messageId":"...","model":"mai-code-1.1-flash","content":"PONG","phase":"final_answer",...}}
      ```
    - Summary event:
      ```json
      {"type":"result","timestamp":"...","sessionId":"...","exitCode":0,"usage":{"premiumRequests":0,"totalApiDurationMs":2442,"sessionDurationMs":6751,"codeChanges":{"linesAdded":0,"linesRemoved":0,"filesModified":[]}}}
      ```
- **Structured Output**:
  - **No CLI `--json-schema` flag**.
  - **Strategy**: Inject the JSON schema instructions directly into the prompt (e.g. `Respond ONLY with a valid JSON object matching schema: ...`).
  - Extract the answer using `-s` in text mode or from `assistant.message` in `json` mode.
  - In `jarvis/providers/`, parse the JSON response with standard retry/repair if markdown fencing (```json ... ```) is present.
- **Permissions & Isolation**:
  - `--allow-all` / `--yolo`: Enables `--allow-all-tools`, `--allow-all-paths`, and `--allow-all-urls`.
  - `--allow-all-tools`: Non-interactive tool approval (also via env var `COPILOT_ALLOW_ALL=true`).
  - `-C <directory>`: Sets the target working directory.
  - Fine-grained controls:
    - `--allow-tool='shell(git:*)'`, `--deny-tool='shell(git push)'`.
    - `--available-tools[=tools...]`, `--excluded-tools[=tools...]`.
    - `--no-ask-user`: Disables user prompts during execution.
- **Model Selection**:
  - `--model <model>`:
    - Supported model IDs (configured in CLI catalog): `claude-sonnet-5`, `claude-sonnet-4.6`, `claude-haiku-4.5`, `claude-opus-4.8`, `claude-opus-4.6`, `gpt-5.6-sol`, `gpt-5.5`, `gpt-5.4`, `gpt-5.3-codex`, `gemini-3.5-flash`, etc.
    - Default/Fallback: `--model auto` allows Copilot to resolve the best eligible model for the authenticated subscription tier (e.g., `mai-code-1.1-flash`).
    - If a model not allowed by the user's SKU is passed, Copilot exits with code 1:
      `Error: Model "<name>" from --model flag is not available.`
- **Quota & Usage**:
  - Standalone quota command: No dedicated CLI command exists to query global remaining quota non-interactively (the interactive `/usage` command is treated as a prompt in `-p` mode).
  - Turn-level accounting:
    - Text mode writes session AI credits and token count to `stderr`.
    - JSON mode provides `usage.totalApiDurationMs`, `sessionDurationMs`, and `premiumRequests` in the final `result` line.
  - Budget cap:
    - `--max-ai-credits <credits>` (minimum `30`) sets a hard cap on AI credit consumption for the run.
- **Error Detection**:
  - **Not installed**: `shutil.which("copilot") is None` or `FileNotFoundError`.
  - **Not logged in**:
    - Exit code: `1`.
    - Output (verified by fixture `tests/fixtures/providers/copilot/not_logged_in.txt`):
      ```text
      Error: No authentication information found.

      Copilot can be authenticated with GitHub using an OAuth Token or a Fine-Grained Personal Access Token.
      To authenticate, you can use any of the following methods:
        • Start 'copilot' and run the '/login' command
        • Set the COPILOT_GITHUB_TOKEN, GH_TOKEN, or GITHUB_TOKEN environment variable
        • Run 'gh auth login' to authenticate with the GitHub CLI
      ```
  - **Rate limited / Limits reached**:
    - Exit code != 0, output contains `Session limits:` or `rate limit` (heuristic).

---

## 3. Sample Fixtures

Recorded CLI outputs are committed under `tests/fixtures/providers/`:

```
tests/fixtures/providers/
├── agy/
│   ├── json_output.json
│   ├── structured_output.json
│   ├── text_output.txt
│   └── usage_output.json
├── claude/
│   ├── json_output.json
│   ├── structured_output.json
│   ├── text_output.txt
│   └── usage_output.json
└── copilot/
    ├── jsonl_output.jsonl
    ├── not_logged_in.txt
    ├── stderr_stats.txt
    ├── structured_output.txt
    ├── text_output.txt
    └── text_silent_output.txt
```

---

## 4. Implementation Guidelines for Phase 1 (`jarvis/providers/`) — Initial Proposal (Non-binding)

*(Note: This is an initial proposal based on spike findings. The definitive provider interface will be designed and implemented in Issue #8).*

1. **Provider Adapter Interface (Draft)**:
   ```python
   class Provider(ABC):
       name: str
       capabilities: set[str]  # {"execute", "plan_structured", "review", "quota"}

       @abstractmethod
       def build_exec_cmd(self, prompt: str, project: dict, limits: dict) -> list[str]: ...

       @abstractmethod
       def build_plan_cmd(self, prompt: str, schema: dict, model: str) -> list[str]: ...

       @abstractmethod
       def build_review_cmd(self, prompt: str, schema: dict, model: str, timeout_min: int) -> list[str]: ...

       @abstractmethod
       def parse_output(self, code: int, stdout: str, stderr: str) -> ProviderResult: ...

       @abstractmethod
       def check_installed_and_logged_in(self) -> tuple[bool, str | None]: ...
   ```

2. **Structured Output Strategy**:
   - For `claude` and `agy`: Pass `--output-format json --json-schema <schema>` and extract `data["structured_output"]`.
   - For `copilot`: Pass `--silent` with schema description in the system prompt; parse resulting JSON with markdown fence stripping and one validation retry.

3. **Quota Snapshot Strategy**:
   - `agy`: Full bucket tracking via `agy -p /usage --output-format json`.
   - `claude`: Session and weekly tracking via `claude -p /usage --output-format json`.
   - `copilot`: Record turn credit usage from stderr / result JSON. For quota exhaustion, detect rate-limit errors on run execution.
