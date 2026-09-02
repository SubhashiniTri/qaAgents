"""Executes the generated pytest module and maps results back to test cases."""
from __future__ import annotations

import logging
import os
import subprocess
import sys
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path

from generator import TestCase

log = logging.getLogger(__name__)

TEST_TIMEOUT_SEC = 600


@dataclass
class CaseResult:
    key: str
    status: str            # passed | failed | error | skipped | untested
    elapsed_sec: float
    message: str

    @property
    def emoji(self) -> str:
        return {"passed": ":white_check_mark:", "failed": ":x:",
                "error": ":rotating_light:", "skipped": ":fast_forward:"}.get(self.status, ":grey_question:")


def write_test_module(workdir: str, code: str) -> Path:
    d = Path(workdir)
    d.mkdir(parents=True, exist_ok=True)
    path = d / "test_generated.py"
    path.write_text(code, encoding="utf-8")
    return path


def _python_with_pytest() -> str:
    """Return a Python executable that has pytest; falls back to sys.executable."""
    root = Path(__file__).resolve().parent
    candidates = [
        sys.executable,
        # project venv — Windows
        str(root / ".venv" / "Scripts" / "python.exe"),
        # project venv — Unix
        str(root / ".venv" / "bin" / "python"),
    ]
    for py in candidates:
        if not Path(py).exists():
            continue
        try:
            r = subprocess.run([py, "-c", "import pytest"], capture_output=True, timeout=5)
            if r.returncode == 0:
                log.debug("Using Python for pytest: %s", py)
                return py
        except Exception:
            continue
    log.warning("Could not find a Python with pytest; using %s", sys.executable)
    return sys.executable


def run_pytest(workdir: str, test_path: Path) -> tuple[int, str, Path]:
    junit = Path(workdir) / "junit.xml"
    python = _python_with_pytest()
    cmd = [python, "-m", "pytest", str(test_path), "-v",
           f"--junitxml={junit}", "-p", "no:cacheprovider"]
    log.info("Running: %s", " ".join(cmd))
    env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}
    try:
        proc = subprocess.run(cmd, cwd=workdir, capture_output=True, text=True,
                              timeout=TEST_TIMEOUT_SEC, env=env)
        output = proc.stdout + proc.stderr
        return proc.returncode, output, junit
    except subprocess.TimeoutExpired:
        return 124, f"pytest timed out after {TEST_TIMEOUT_SEC}s", junit


def parse_junit(junit: Path, cases: list[TestCase]) -> list[CaseResult]:
    by_key = {c.key: c for c in cases}
    found: dict[str, CaseResult] = {}

    if junit.exists():
        root = ET.parse(junit).getroot()
        for tc in root.iter("testcase"):
            name = tc.get("name", "")
            raw_key = name[len("test_"):] if name.startswith("test_") else name
            raw_key = raw_key.split("[", 1)[0]  # strip pytest parametrize suffix
            # Exact match first; then fall back to longest case key that prefixes the name.
            if raw_key in by_key:
                key = raw_key
            else:
                candidates = [k for k in by_key if raw_key.startswith(k)]
                if not candidates:
                    continue
                key = max(candidates, key=len)
            elapsed = float(tc.get("time", "0") or 0)
            failure = tc.find("failure")
            error = tc.find("error")
            skipped = tc.find("skipped")
            if failure is not None:
                status, msg = "failed", (failure.get("message") or failure.text or "")
            elif error is not None:
                status, msg = "error", (error.get("message") or error.text or "")
            elif skipped is not None:
                status, msg = "skipped", (skipped.get("message") or "")
            else:
                status, msg = "passed", "Assertions passed."
            prev = found.get(key)
            if prev and prev.status in ("failed", "error"):
                continue  # keep the worst result
            found[key] = CaseResult(key, status, elapsed, (msg or "").strip()[:4000])

    results = []
    for c in cases:
        results.append(found.get(c.key, CaseResult(
            c.key, "untested", 0.0, "No matching pytest result found for this case.")))
    return results


def summarize(results: list[CaseResult]) -> dict:
    counts = {"passed": 0, "failed": 0, "error": 0, "skipped": 0, "untested": 0}
    for r in results:
        counts[r.status] = counts.get(r.status, 0) + 1
    counts["total"] = len(results)
    counts["ok"] = counts["failed"] == 0 and counts["error"] == 0 and counts["untested"] == 0
    return counts
