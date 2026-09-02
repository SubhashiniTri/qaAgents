"""Minimal TestRail API v2 client: add cases, create run, push results."""
from __future__ import annotations

import logging
import time

import requests

from config import TestRailConfig
from generator import TestCase

log = logging.getLogger(__name__)

# TestRail built-in status ids
STATUS_PASSED = 1
STATUS_BLOCKED = 2
STATUS_UNTESTED = 3
STATUS_RETEST = 4
STATUS_FAILED = 5

PRIORITY_MAP = {"Low": 1, "Medium": 2, "High": 3, "Critical": 4}


class TestRailError(RuntimeError):
    pass


class TestRailClient:
    def __init__(self, cfg: TestRailConfig):
        self.cfg = cfg
        self.s = requests.Session()
        self.s.auth = (cfg.user, cfg.api_key)
        self.s.headers.update({"Content-Type": "application/json"})

    def _call(self, method: str, endpoint: str, payload: dict | None = None, retries: int = 3):
        url = f"{self.cfg.base_url}/index.php?/api/v2/{endpoint}"
        for attempt in range(retries):
            r = self.s.request(method, url, json=payload, timeout=45)
            if r.status_code == 429:  # rate limited
                wait = int(r.headers.get("Retry-After", 2 ** attempt))
                log.warning("TestRail rate limited; retrying in %ss", wait)
                time.sleep(wait)
                continue
            if r.status_code >= 400:
                raise TestRailError(f"{method} {endpoint} -> {r.status_code}: {r.text[:400]}")
            return r.json() if r.content else {}
        raise TestRailError(f"{method} {endpoint} failed after {retries} attempts")

    # ---- cases ----

    def add_case(self, case: TestCase, run_label: str) -> int:
        steps = [{"content": s.step, "expected": s.expected} for s in case.steps]
        payload: dict = {
            "title": case.title[:250],
            "custom_preconds": case.preconditions,
            "custom_steps_separated": steps,
            "custom_automation_key": case.key,
            "refs": case.requirement_source[:250],
        }
        pid = self.cfg.priority_id or PRIORITY_MAP.get(case.priority)
        if pid:
            payload["priority_id"] = pid
        if self.cfg.template_id:
            payload["template_id"] = self.cfg.template_id
        if self.cfg.type_id:
            payload["type_id"] = self.cfg.type_id

        try:
            res = self._call("POST", f"add_case/{self.cfg.section_id}", payload)
        except TestRailError as e:
            # custom_automation_key / refs are not present in every TestRail instance
            if "custom_automation_key" in str(e) or "refs" in str(e):
                payload.pop("custom_automation_key", None)
                payload.pop("refs", None)
                res = self._call("POST", f"add_case/{self.cfg.section_id}", payload)
            else:
                raise
        case.testrail_case_id = res["id"]
        log.info("TestRail case C%s created: %s", res["id"], case.title)
        return res["id"]

    def upload_cases(self, cases: list[TestCase], run_label: str) -> list[int]:
        return [self.add_case(c, run_label) for c in cases]

    # ---- runs ----

    def add_run(self, name: str, description: str, case_ids: list[int]) -> int:
        payload = {
            "name": name[:250],
            "description": description[:8000],
            "include_all": False,
            "case_ids": case_ids,
        }
        if self.cfg.suite_id:
            payload["suite_id"] = self.cfg.suite_id
        res = self._call("POST", f"add_run/{self.cfg.project_id}", payload)
        log.info("TestRail run R%s created", res["id"])
        return res["id"]

    def add_results(self, run_id: int, results: list[dict]) -> None:
        """results: [{case_id, status_id, comment, elapsed?}]"""
        if not results:
            return
        self._call("POST", f"add_results_for_cases/{run_id}", {"results": results})
        log.info("Pushed %d results to run R%s", len(results), run_id)

    def close_run(self, run_id: int) -> None:
        self._call("POST", f"close_run/{run_id}", {})

    def run_url(self, run_id: int) -> str:
        return f"{self.cfg.base_url}/index.php?/runs/view/{run_id}"

    def case_url(self, case_id: int) -> str:
        return f"{self.cfg.base_url}/index.php?/cases/view/{case_id}"
