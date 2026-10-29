"""Client GitHub minimo (REST + GraphQL) per i comandi da Telegram.

Espone solo le operazioni che Jarvis può fare: creare issue, assegnarle, aggiungerle a un
progetto (Projects v2) e cambiarne lo stato. Niente cancellazioni, niente impostazioni di repo.
"""

import json
import urllib.error
import urllib.request

from jarvis.i18n import t

API = "https://api.github.com"

PROJECTS_FIELDS = """
  projectsV2(first: 40, orderBy: {field: UPDATED_AT, direction: DESC}) {
    nodes {
      id number title closed
      field(name: "Status") { ... on ProjectV2SingleSelectField { id options { id name } } }
    }
  }
"""


class GitHubError(RuntimeError):
    pass


class GitHub:
    def __init__(self, token: str, timeout: int = 30):
        if not token:
            raise ValueError("token GitHub mancante")
        self.token = token
        self.timeout = timeout

    def _request(self, method: str, path: str, body: dict | None = None) -> dict:
        return self._call(method, path, body)[0]

    def _call(self, method: str, path: str, body: dict | None = None) -> tuple[dict, dict]:
        req = urllib.request.Request(
            API + path, method=method, data=json.dumps(body).encode() if body is not None else None,
            headers={
                "Authorization": f"Bearer {self.token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": "jarvis",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                return json.load(r), dict(r.headers)
        except urllib.error.HTTPError as e:
            detail = e.read().decode(errors="replace")[:300]
            raise GitHubError(f"GitHub {method} {path}: HTTP {e.code} {detail}") from None
        except urllib.error.URLError as e:
            raise GitHubError(f"GitHub {method} {path}: {e.reason}") from None

    def token_expiration(self) -> str | None:
        """Scadenza del token (YYYY-MM-DD) letta dall'header di GitHub; None se il token non scade.

        /rate_limit non consuma richieste del limite orario.
        """
        _, headers = self._call("GET", "/rate_limit")
        value = {k.lower(): v for k, v in headers.items()}.get("github-authentication-token-expiration")
        return value[:10] if value else None

    def graphql(self, query: str, **variables) -> dict:
        out = self._request("POST", "/graphql", {"query": query, "variables": variables})
        if out.get("errors"):
            raise GitHubError(f"GitHub GraphQL: {json.dumps(out['errors'])[:300]}")
        return out["data"]

    def projects(self, owner: str) -> list[dict]:
        """Progetti aperti (fra i 40 aggiornati più di recente) di un utente o di un'org, con le opzioni del campo Status."""
        data = self.graphql(
            f"query($login: String!) {{ repositoryOwner(login: $login) {{ "
            f"... on User {{ {PROJECTS_FIELDS} }} ... on Organization {{ {PROJECTS_FIELDS} }} }} }}",
            login=owner,
        )
        nodes = ((data.get("repositoryOwner") or {}).get("projectsV2") or {}).get("nodes") or []
        return [
            {
                "owner": owner, "number": p["number"], "title": p["title"], "id": p["id"],
                "status_field": (p.get("field") or {}).get("id"),
                "statuses": {o["name"]: o["id"] for o in (p.get("field") or {}).get("options", [])},
            }
            for p in nodes if p and not p["closed"]
        ]

    def create_issue(self, repo: str, title: str, body: str, labels: list[str], assignees: list[str]) -> dict:
        return self._request("POST", f"/repos/{repo}/issues",
                             {"title": title, "body": body, "labels": labels, "assignees": assignees})

    def add_assignees(self, repo: str, number: int, assignees: list[str]) -> dict:
        return self._request("POST", f"/repos/{repo}/issues/{number}/assignees", {"assignees": assignees})

    def close_issue(self, repo: str, number: int) -> dict:
        return self._request("PATCH", f"/repos/{repo}/issues/{number}", {"state": "closed"})

    def pull(self, repo: str, number: int) -> dict:
        return self._request("GET", f"/repos/{repo}/pulls/{number}")

    def pull_commits(self, repo: str, number: int) -> list[dict]:
        return self._request("GET", f"/repos/{repo}/pulls/{number}/commits?per_page=100")

    def pull_reviews(self, repo: str, number: int) -> list[dict]:
        return self._request("GET", f"/repos/{repo}/pulls/{number}/reviews?per_page=100")

    def pull_comments(self, repo: str, number: int) -> list[dict]:
        """Commenti inline della review (sulle righe del diff)."""
        return self._request("GET", f"/repos/{repo}/pulls/{number}/comments?per_page=100")

    def issue_comments(self, repo: str, number: int) -> list[dict]:
        """Commenti nella conversazione della PR."""
        return self._request("GET", f"/repos/{repo}/issues/{number}/comments?per_page=100")

    def check_runs(self, repo: str, sha: str) -> list[dict]:
        return self._request("GET", f"/repos/{repo}/commits/{sha}/check-runs?per_page=100").get("check_runs", [])

    def statuses(self, repo: str, sha: str) -> list[dict]:
        """Stati classici (commit status API), l'ultimo per contesto."""
        return self._request("GET", f"/repos/{repo}/commits/{sha}/status").get("statuses", [])

    def issue(self, repo: str, number: int) -> dict:
        """node_id, url, titolo e item nei progetti dell'issue."""
        owner, name = repo.split("/")
        data = self.graphql(
            """query($owner: String!, $name: String!, $number: Int!) {
                 repository(owner: $owner, name: $name) { issue(number: $number) {
                   id url title state projectItems(first: 20) { nodes { id project { id } } } } } }""",
            owner=owner, name=name, number=number,
        )
        issue = (data.get("repository") or {}).get("issue")
        if issue is None:
            raise GitHubError(t("github.err.issue_not_found", repo=repo, number=number))
        return issue

    def add_to_project(self, project_id: str, content_id: str) -> str:
        data = self.graphql(
            """mutation($project: ID!, $content: ID!) {
                 addProjectV2ItemById(input: {projectId: $project, contentId: $content}) { item { id } } }""",
            project=project_id, content=content_id,
        )
        return data["addProjectV2ItemById"]["item"]["id"]

    def set_status(self, project_id: str, item_id: str, field_id: str, option_id: str) -> None:
        self.graphql(
            """mutation($project: ID!, $item: ID!, $field: ID!, $option: String!) {
                 updateProjectV2ItemFieldValue(input: {projectId: $project, itemId: $item, fieldId: $field,
                   value: {singleSelectOptionId: $option}}) { projectV2Item { id } } }""",
            project=project_id, item=item_id, field=field_id, option=option_id,
        )
