"""Fetches requirement documents from Jira via the REST API (Cloud v3 + Server v2)."""
from __future__ import annotations

import logging
from dataclasses import dataclass

import requests

from config import JiraConfig
from github_reader import RequirementDoc

log = logging.getLogger(__name__)

MAX_RESULTS = 20


def _adf_to_text(node: dict | str | None) -> str:
    """Convert Atlassian Document Format (Cloud) to plain text recursively."""
    if not node:
        return ""
    if isinstance(node, str):
        return node
    t = node.get("type", "")
    content = node.get("content", [])
    if t == "text":
        return node.get("text", "")
    if t == "hardBreak":
        return "\n"
    if t == "heading":
        level = node.get("attrs", {}).get("level", 1)
        return "#" * level + " " + "".join(_adf_to_text(c) for c in content) + "\n"
    if t == "paragraph":
        return "".join(_adf_to_text(c) for c in content) + "\n"
    if t == "bulletList":
        return "".join("- " + _adf_to_text(c) for c in content)
    if t == "orderedList":
        return "".join(f"{i + 1}. " + _adf_to_text(c) for i, c in enumerate(content))
    if t == "listItem":
        return "".join(_adf_to_text(c) for c in content)
    if t == "codeBlock":
        return "```\n" + "".join(_adf_to_text(c) for c in content) + "```\n"
    # doc, table, tableRow, tableCell, blockquote, etc. — recurse into children
    return "".join(_adf_to_text(c) for c in content)


def _field_text(value) -> str:
    """Extract plain text from a field value (string, ADF dict, or None)."""
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, dict):
        if value.get("type") == "doc":
            return _adf_to_text(value).strip()
        # Server-style wiki markup — just take the raw string if present
        return str(value.get("content") or "").strip()
    return ""


# Common names Jira teams use for the acceptance-criteria custom field.
_AC_FIELD_NAMES = frozenset({
    "acceptance criteria", "acceptancecriteria", "acceptance_criteria",
    "ac", "definition of done", "dod",
})


class JiraReader:
    def __init__(self, cfg: JiraConfig):
        self.cfg = cfg
        self.s = requests.Session()
        # Jira Cloud: email + API token via Basic auth.
        self.s.auth = (cfg.user, cfg.api_token)
        self.s.headers.update({"Accept": "application/json"})
        # Cloud uses /rest/api/3, Server/DC uses /rest/api/2.
        version = "3" if cfg.api_version == "3" else "2"
        self._api = f"{cfg.base_url.rstrip('/')}/rest/api/{version}"

    def _get(self, path: str, **params):
        r = self.s.get(f"{self._api}/{path}", params=params or None, timeout=30)
        r.raise_for_status()
        return r.json()

    def _to_doc(self, data: dict) -> RequirementDoc:
        key = data["key"]
        fields = data.get("fields", {})
        summary = fields.get("summary", key)
        issue_type = (fields.get("issuetype") or {}).get("name", "")
        priority = (fields.get("priority") or {}).get("name", "")
        status = (fields.get("status") or {}).get("name", "")

        description = _field_text(fields.get("description"))

        # Try to find acceptance criteria in any custom field by label name.
        ac = ""
        for fname, fval in fields.items():
            label = str(fname).lower().replace(" ", "").replace("-", "").replace("_", "")
            if label in _AC_FIELD_NAMES or "acceptance" in label:
                ac = _field_text(fval)
                if ac:
                    break

        lines = [f"# [{key}] {summary}"]
        if issue_type:
            lines.append(f"Type: {issue_type}")
        if priority:
            lines.append(f"Priority: {priority}")
        if status:
            lines.append(f"Status: {status}")
        if description:
            lines.append(f"\n## Description\n{description}")
        if ac:
            lines.append(f"\n## Acceptance Criteria\n{ac}")

        return RequirementDoc(
            source=f"jira:{key}",
            title=f"[{key}] {summary}",
            text="\n".join(lines),
        )

    def fetch_issue(self, issue_key: str) -> RequirementDoc:
        log.info("Fetching Jira issue %s", issue_key)
        data = self._get(f"issue/{issue_key}")
        return self._to_doc(data)

    def fetch_by_jql(self, jql: str, max_results: int = MAX_RESULTS) -> list[RequirementDoc]:
        log.info("Fetching Jira issues via JQL: %s", jql)
        data = self._get("search", jql=jql, maxResults=max_results,
                         fields="summary,description,issuetype,priority,status,*all")
        docs = [self._to_doc(issue) for issue in data.get("issues", [])]
        log.info("Fetched %d Jira issues", len(docs))
        return docs
