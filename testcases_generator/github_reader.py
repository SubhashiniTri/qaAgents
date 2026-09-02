"""Reads requirement text out of the GitHub repo (contents API + optional issues)."""
from __future__ import annotations

import base64
import logging
from dataclasses import dataclass

import requests

from config import GitHubConfig

log = logging.getLogger(__name__)

TEXT_EXT = (".md", ".markdown", ".txt", ".rst", ".adoc", ".feature")
MAX_FILE_BYTES = 200_000
MAX_FILES = 40


@dataclass
class RequirementDoc:
    source: str      # e.g. "docs/auth.md" or "issue#42"
    title: str
    text: str

    def as_prompt_block(self) -> str:
        return f"<document source=\"{self.source}\" title=\"{self.title}\">\n{self.text}\n</document>"


class GitHubReader:
    def __init__(self, cfg: GitHubConfig):
        self.cfg = cfg
        self.s = requests.Session()
        self.s.headers.update({
            "Authorization": f"Bearer {cfg.token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "testcase-agent",
        })

    def _url(self, path: str) -> str:
        return f"{self.cfg.api_base}/repos/{self.cfg.owner}/{self.cfg.repo}{path}"

    def _get(self, path: str, **params):
        r = self.s.get(self._url(path), params=params or None, timeout=30)
        if r.status_code == 404:
            return None
        r.raise_for_status()
        return r.json()

    # ---- contents ----

    def _walk(self, path: str, out: list[dict], depth: int = 0) -> None:
        if len(out) >= MAX_FILES or depth > 4:
            return
        data = self._get(f"/contents/{path.strip('/')}", ref=self.cfg.ref)
        if data is None:
            log.warning("Path not found in repo: %s", path)
            return
        entries = data if isinstance(data, list) else [data]
        for e in entries:
            if len(out) >= MAX_FILES:
                return
            if e["type"] == "dir":
                self._walk(e["path"], out, depth + 1)
            elif e["type"] == "file" and e["name"].lower().endswith(TEXT_EXT):
                if e.get("size", 0) <= MAX_FILE_BYTES:
                    out.append(e)

    def _read_file(self, entry: dict) -> str:
        if entry.get("content") and entry.get("encoding") == "base64":
            return base64.b64decode(entry["content"]).decode("utf-8", "replace")
        blob = self._get(f"/contents/{entry['path']}", ref=self.cfg.ref)
        if blob and blob.get("encoding") == "base64":
            return base64.b64decode(blob["content"]).decode("utf-8", "replace")
        return ""

    # ---- issues ----

    def _issues(self) -> list[RequirementDoc]:
        if not self.cfg.issue_label:
            return []
        items = self._get("/issues", state="open", labels=self.cfg.issue_label, per_page=30) or []
        docs = []
        for it in items:
            if "pull_request" in it:
                continue
            docs.append(RequirementDoc(
                source=f"issue#{it['number']}",
                title=it.get("title", ""),
                text=(it.get("body") or "").strip(),
            ))
        return docs

    # ---- public ----

    def fetch_issue(self, issue_number: int) -> list[RequirementDoc]:
        """Return a single issue as a RequirementDoc list."""
        data = self._get(f"/issues/{issue_number}")
        if data is None:
            raise RuntimeError(
                f"Issue #{issue_number} not found in {self.cfg.owner}/{self.cfg.repo}."
            )
        if "pull_request" in data:
            raise RuntimeError(f"#{issue_number} is a pull request, not an issue.")
        return [RequirementDoc(
            source=f"issue#{data['number']}",
            title=data.get("title", ""),
            text=(data.get("body") or "").strip(),
        )]

    def fetch(self) -> list[RequirementDoc]:
        entries: list[dict] = []
        for p in self.cfg.paths:
            self._walk(p, entries)

        docs: list[RequirementDoc] = []
        seen = set()
        for e in entries:
            if e["path"] in seen:
                continue
            seen.add(e["path"])
            text = self._read_file(e).strip()
            if text:
                docs.append(RequirementDoc(source=e["path"], title=e["name"], text=text))

        docs.extend(self._issues())
        log.info("Collected %d requirement documents from %s/%s",
                 len(docs), self.cfg.owner, self.cfg.repo)
        if not docs:
            raise RuntimeError(
                f"No requirement documents found in {self.cfg.owner}/{self.cfg.repo} "
                f"under paths {self.cfg.paths} (ref={self.cfg.ref})."
            )
        return docs

    def repo_slug(self) -> str:
        return f"{self.cfg.owner}/{self.cfg.repo}@{self.cfg.ref}"
