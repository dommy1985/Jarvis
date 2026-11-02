"""Segreti da non far uscire nei log, nelle PR e nei messaggi fra agenti."""

import re

SECRET = re.compile(r"ghp_\w{16,}|github_pat_\w{20,}|sk-[\w-]{20,}|AIza[\w-]{30,}|Bearer\s+\S+|"
                    r"(?i:(?:token|password|passwd|secret|api[_-]?key)\s*[=:]\s*)\S+")


def mask(text: str) -> str:
    return SECRET.sub("***", text)
