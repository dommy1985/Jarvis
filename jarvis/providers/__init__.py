"""Registro dei provider, indicizzato dal campo `cli` degli esecutori in config/projects.yaml."""

from jarvis.providers.base import CAPABILITIES, QUOTA, Provider, ProviderError, last_result, run_structured, tail
from jarvis.providers.agy import AgyProvider
from jarvis.providers.claude import ClaudeProvider
from jarvis.providers.copilot import CopilotProvider
from jarvis.providers.structured import call_structured

REGISTRY: dict[str, Provider] = {}


def register(provider: Provider) -> Provider:
    REGISTRY[provider.name] = provider
    return provider


def get(cli: str) -> Provider | None:
    return REGISTRY.get(cli)


def can(cli: str | None, capability: str) -> bool:
    """True se `cli` è un provider registrato con quella capability."""
    provider = REGISTRY.get(cli)
    return provider is not None and capability in provider.capabilities


register(ClaudeProvider())
register(AgyProvider())
register(CopilotProvider())

__all__ = ["CAPABILITIES", "QUOTA", "REGISTRY", "Provider", "ProviderError", "call_structured", "can", "get",
           "last_result", "register", "run_structured", "tail"]
