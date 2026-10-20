"""Carica e valida config/projects.yaml."""

from dataclasses import dataclass
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml

from jarvis import i18n, providers
from jarvis.i18n import t


RUNNER_DEFAULTS = {
    "worktrees": "~/.jarvis/worktrees",
    "setup_timeout_minutes": 30,
    "agent_timeout_minutes": 60,
    "test_timeout_minutes": 45,
    "stale_hours": 3,  # run senza notizie dal runner oltre questo limite: fallito
}

DEFAULT_OWNER_NAME = "Owner"
DEFAULT_INGEST_HOURS = (7, 20)  # primo e ultimo giro del cron di ingest (ora locale): "0 7-20 * * *"


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class Config:
    raw: dict

    @property
    def projects(self) -> dict:
        return self.raw["projects"]

    @property
    def eligible_label(self) -> str:
        return self.raw["defaults"]["eligible_label"]

    @property
    def eligible_if_assigned(self) -> set[str]:
        return set(self.raw["defaults"].get("eligible_if_assigned", []))

    @property
    def owner_github(self) -> str:
        return self.raw["owner"]["github"]

    @property
    def owner_name(self) -> str:
        return self.raw["owner"].get("name") or DEFAULT_OWNER_NAME

    @property
    def locale(self) -> str:
        return self.raw["owner"].get("locale") or i18n.DEFAULT

    @property
    def owner_desc(self) -> str:
        """Nome dell'owner con il profilo fra parentesi, se c'è (per i prompt)."""
        profile = self.raw["owner"].get("profile")
        return f"{self.owner_name} ({profile})" if profile else self.owner_name

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.raw["owner"].get("timezone") or "UTC")

    @property
    def ingest_hours(self) -> range:
        first, last = self.raw["ingest"].get("hours") or DEFAULT_INGEST_HOURS
        return range(first, last + 1)

    def fill(self, prompt: str) -> str:
        """Sostituisce {owner} e {owner_desc} in un prompt."""
        return prompt.replace("{owner_desc}", self.owner_desc).replace("{owner}", self.owner_name)

    @property
    def planner(self) -> dict:
        return self.raw["planner"]

    @property
    def runner(self) -> dict:
        return {**RUNNER_DEFAULTS, **(self.raw.get("runner") or {})}

    @property
    def commands(self) -> dict | None:
        return self.raw.get("commands")

    def fallback_chain(self, executor: str) -> list[str]:
        """Esecutori da provare, in ordine, quando `executor` ha la quota esaurita o la CLI non è pronta.

        `fallbacks` (chiave: nome dell'esecutore o famiglia, valore: elenco ordinato) oppure, come prima,
        `counterparts` (famiglia -> un esecutore)."""
        family = self.raw["executors"].get(executor, {}).get("family")
        fallbacks = self.raw.get("fallbacks") or {}
        for key in (executor, family):
            if key in fallbacks:
                return [e for e in fallbacks[key] if e != executor]
        legacy = (self.raw.get("counterparts") or {}).get(family)
        return [legacy] if legacy else []

    def counterpart(self, executor: str) -> str | None:
        """Revisore del lavoro di `executor`: il primo del fallback di un'altra famiglia (review incrociata)."""
        executors = self.raw["executors"]
        family = executors.get(executor, {}).get("family")
        return next((e for e in self.fallback_chain(executor) if executors[e].get("family") != family), None)

    @property
    def review(self) -> dict:
        return {"fix_rounds": 1, "timeout_minutes": 15, **(self.raw.get("review") or {})}

    @property
    def outcomes(self) -> dict:
        return {"check_minutes": 10, **(self.raw.get("outcomes") or {})}

    def focus_projects(self, names: list[str] | None) -> set[str] | None:
        """Progetti su cui si concentra un briefing, con i submodule dei progetti scelti (my-app include
        my-lib). None: nessun focus."""
        if not names:
            return None
        chosen = set(names)
        for name in names:
            chosen.update((self.projects.get(name, {}).get("submodules") or {}).values())
        return chosen

    def submodule_parent(self, project: str | None) -> tuple[str, str] | None:
        """(progetto che include `project` come submodule, path del submodule), se c'è."""
        for name, p in self.projects.items():
            for path, child in (p.get("submodules") or {}).items():
                if child == project:
                    return name, path
        return None

    def writable_repos(self) -> list[str]:
        owners = {o.lower() for o in (self.commands or {}).get("write_owners", [])}
        return [r for r in self.github_repos() if r.split("/")[0].lower() in owners]

    @property
    def available_hours(self) -> float:
        return float(self.raw["defaults"]["available_hours"])

    def executor_for(self, project: str | None, task_type: str | None, proposed: str) -> tuple[str, str]:
        """Applica la regola di progetto alla proposta del planner: (esecutore, fonte della decisione)."""
        if self.raw["executors"].get(proposed, {}).get("human"):
            return proposed, "planner"  # "la faccio io": la regola di progetto sceglie solo fra agenti
        override = self.projects.get(project, {}).get("override", {}) if project else {}
        if task_type in override:
            return override[task_type], "project_rule"
        return proposed, "planner"

    def project_for_jira(self, jira_project: str) -> str | None:
        for name, p in self.projects.items():
            if jira_project in p.get("sources", {}).get("jira", []):
                return name
        return None

    def project_for_repo(self, repo: str) -> str | None:
        for name, p in self.projects.items():
            if repo.lower() in (r.lower() for r in p.get("sources", {}).get("github", [])):
                return name
        return None

    def github_repos(self) -> list[str]:
        return [r for p in self.projects.values() for r in p.get("sources", {}).get("github", [])]

    def sources(self) -> dict:
        """Query di ingest per n8n, derivate dai progetti configurati."""
        owner = self.owner_github.lower()
        personal = [r for r in self.github_repos() if r.split("/")[0].lower() == owner]
        team = [r for r in self.github_repos() if r.split("/")[0].lower() != owner]

        def query(repos, extra=""):
            return " ".join(["is:open is:issue", *(f"repo:{r}" for r in repos)]) + extra

        assignee = f" assignee:{self.owner_github}" if self.raw["ingest"].get("personal_assignee_only") else ""
        return {
            "jira_jql": self.raw["ingest"]["jira_jql"],
            "github_queries": {"team": query(team), "personal": query(personal, assignee)},
        }


def load(path: str | Path) -> Config:
    raw = yaml.safe_load(Path(path).read_text())
    declared = (raw.get("owner") or {}).get("locale") or i18n.DEFAULT
    if declared in i18n.available():  # gli errori che seguono sono nella lingua dell'owner
        i18n.set_locale(declared)
    for key in ("defaults", "owner", "ingest", "planner", "executors", "routing", "projects"):
        if key not in raw:
            raise ConfigError(t("config.err.missing_section", key=key))

    owner = raw["owner"]
    for field in ("name", "profile"):
        if not isinstance(owner.get(field) or "", str):
            raise ConfigError(t("config.err.owner_string", field=field))
    if not isinstance(owner.get("locale") or "", str) or (owner.get("locale") or i18n.DEFAULT) not in i18n.available():
        raise ConfigError(t("config.err.locale", locale=owner.get("locale"), available=", ".join(i18n.available())))
    try:
        ZoneInfo(owner.get("timezone") or "UTC")
    except (ZoneInfoNotFoundError, ValueError) as e:
        raise ConfigError(t("config.err.timezone", timezone=owner["timezone"])) from e
    hours = raw["ingest"].get("hours")
    if hours is not None and not (isinstance(hours, list) and len(hours) == 2 and all(type(h) is int for h in hours)
                                  and 0 <= hours[0] <= hours[1] <= 23):
        raise ConfigError(t("config.err.ingest_hours"))

    executors = set(raw["executors"])
    for task_type, executor in raw["routing"].items():
        if executor not in executors:
            raise ConfigError(t("config.err.routing", task_type=task_type, executor=executor))
    for section in ("planner", "commands"):
        if section in raw:
            executor = raw["executors"].get((raw[section] or {}).get("executor"), {})
            if providers.get(executor.get("cli")) is None:
                raise ConfigError(t("config.err.section_executor", section=section,
                                    clis=", ".join(sorted(providers.REGISTRY))))
    names = set(executors) | {e.get("family") for e in raw["executors"].values()}
    for key, chain in (raw.get("fallbacks") or {}).items():
        if key not in names:
            raise ConfigError(t("config.err.fallback_key", key=key))
        if not isinstance(chain, list) or len(set(chain)) != len(chain):
            raise ConfigError(t("config.err.fallback_list", key=key))
        for executor in chain:
            if not providers.can(raw["executors"].get(executor, {}).get("cli"), "execute"):
                raise ConfigError(t("config.err.fallback_executor", key=key, executor=executor))
            if executor == key:
                raise ConfigError(t("config.err.fallback_self", key=key))
    for family, executor in (raw.get("counterparts") or {}).items():
        if not providers.can(raw["executors"].get(executor, {}).get("cli"), "execute"):
            raise ConfigError(t("config.err.counterpart_executor", family=family, executor=executor))
        if raw["executors"][executor].get("family") == family:
            raise ConfigError(t("config.err.counterpart_family", family=family, executor=executor))
    for name, p in raw["projects"].items():
        for field in ("path", "sources"):
            if field not in p:
                raise ConfigError(t("config.err.project_field", name=name, field=field))
        for task_type, executor in p.get("override", {}).items():
            if executor not in executors:
                raise ConfigError(t("config.err.override", name=name, task_type=task_type, executor=executor))
    cfg = Config(raw)
    i18n.set_locale(cfg.locale)  # il locale vale per tutto il processo
    return cfg
