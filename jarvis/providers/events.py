"""Righe di log dal vivo: un evento stream-json dell'agente diventa zero o più righe leggibili su Telegram."""

import json


def short(value, root: str | None, limit: int = 150) -> str:
    text = str(value or "").strip().replace("\n", " ")
    if root and text.startswith(root):
        text = text[len(root):].lstrip("/")
    return text if len(text) <= limit else text[:limit - 1] + "…"


def tool_line(name: str, args: dict, root: str | None = None) -> str | None:
    """Una riga per chiamata a uno strumento. I nomi sono quelli di Claude Code e di agy."""
    pick = lambda *keys: next((args[k] for k in keys if args.get(k)), "")  # noqa: E731
    low = name.lower()
    if low in ("read", "view", "view_file", "view_code_item", "view_file_outline"):
        return "📖 Read " + short(pick("file_path", "AbsolutePath", "path"), root)
    if low in ("edit", "multiedit", "write", "notebookedit", "replace_file_content", "write_to_file",
               "multi_replace_file_content", "create"):
        return "✏️ Edit " + short(pick("file_path", "TargetFile", "notebook_path", "path"), root)
    if low in ("grep", "glob", "grep_search", "find_by_name", "list_dir", "ls"):
        return f"🔎 {name} " + short(pick("pattern", "Query", "Pattern", "SearchPath", "DirectoryPath", "path"), root)
    if low in ("bash", "run_command"):
        return "$ " + short(pick("command", "CommandLine"), root, 148)
    if low in ("task", "agent"):
        return "🤖 " + short(pick("description", "prompt"), root, 100)
    if low in ("todowrite", "toolsearch"):
        return None
    return f"🔧 {name}"


def claude_event_lines(event: dict, root: str | None = None) -> list[str]:
    """Righe di log per un evento di `claude -p --output-format stream-json`."""
    if event.get("type") != "assistant":
        return []
    lines = []
    for block in (event.get("message") or {}).get("content") or []:
        if block.get("type") == "tool_use":
            if line := tool_line(str(block.get("name")), block.get("input") or {}, root):
                lines.append(line)
        elif block.get("type") == "text" and str(block.get("text") or "").strip():
            lines.append("💬 " + short(block["text"], root, 160))
    return lines


def agy_event_lines(event: dict, root: str | None = None) -> list[str]:
    """Righe di log per un evento di `agy -p --output-format stream-json` (solo l'avvio degli strumenti)."""
    if event.get("event") != "step_update":
        return []
    step = event.get("step_update") or {}
    if step.get("step_type") == "tool" and step.get("state") == "ACTIVE":
        info = step.get("tool_info") or {}
        if isinstance(info, str):
            try:
                info = json.loads(info)
            except json.JSONDecodeError:
                info = {}
        line = tool_line(str(step.get("tool_name") or info.get("name") or "?"), info.get("parameters") or {}, root)
        return [line] if line else []
    if step.get("step_type") == "agent_response" and str(step.get("text_delta") or "").strip():
        return ["💬 " + short(step["text_delta"], root, 160)]
    return []
