"""Turns requirement docs into structured test cases + executable pytest code."""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, asdict

from config import ModelConfig
from github_reader import RequirementDoc
from providers import ModelProvider, build_provider

log = logging.getLogger(__name__)

MAX_REQ_CHARS = 120_000


@dataclass
class Step:
    step: str
    expected: str


@dataclass
class TestCase:
    key: str                 # stable slug, also the pytest function name suffix
    title: str
    requirement_source: str
    preconditions: str
    steps: list[Step]
    priority: str            # Low | Medium | High | Critical
    testrail_case_id: int | None = None

    def to_dict(self) -> dict:
        d = asdict(self)
        d["steps"] = [asdict(s) if not isinstance(s, dict) else s for s in self.steps]
        return d


# Plain JSON Schema — providers.py wraps this in whichever envelope the SDK expects.
CASE_SCHEMA = {
    "type": "object",
    "properties": {
        "cases": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "key": {
                        "type": "string",
                        "description": "lower_snake_case unique slug, <=40 chars, valid python identifier suffix",
                    },
                    "title": {"type": "string"},
                    "requirement_source": {
                        "type": "string",
                        "description": "source document/issue the case derives from",
                    },
                    "preconditions": {"type": "string"},
                    "steps": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "step": {"type": "string"},
                                "expected": {"type": "string"},
                            },
                            "required": ["step", "expected"],
                        },
                    },
                    "priority": {
                        "type": "string",
                        "enum": ["Low", "Medium", "High", "Critical"],
                    },
                },
                "required": ["key", "title", "requirement_source",
                             "preconditions", "steps", "priority"],
            },
        },
        "pytest_module": {
            "type": "string",
            "description": (
                "Complete runnable Python pytest module. One test function per case, "
                "named test_<key>. Standard library + pytest only unless the repo clearly "
                "provides the dependency. No network calls. Deterministic."
            ),
        },
        "notes": {"type": "string", "description": "assumptions or gaps worth flagging"},
    },
    "required": ["cases", "pytest_module"],
}

SYSTEM = """You are a senior QA engineer generating test cases for the project.

Rules:
- Derive cases ONLY from the supplied requirement documents. Do not invent features.
- Each case must trace to a specific requirement (set requirement_source accordingly).
- Cover a mix: happy path, boundary/edge, negative/error handling.
- Steps must be concrete and observable, never vague ("verify it works" is unacceptable).
- The pytest module must be fully self-contained and RUNNABLE with no external services,
  no network, and no repo imports unless the requirements clearly show an importable module.
  When the real system under test is not importable, encode the requirement as an explicit,
  readable assertion against a small local model/stub defined in the module itself, and add a
  module-level docstring stating that these are requirement-contract tests pending wiring to
  the real SUT. Never write a test that trivially passes (e.g. `assert True`).
- Every test function name must be exactly test_<key> for the matching case key."""


class TestCaseGenerator:
    """Provider-agnostic. Give it a ModelConfig (or an explicit provider) and it returns
    validated TestCase objects plus the pytest module that exercises them."""

    def __init__(self, model_cfg: ModelConfig, num_cases: int = 5,
                 provider: ModelProvider | None = None):
        self.num_cases = num_cases
        self.provider = provider or build_provider(
            model_cfg.provider, model_cfg.api_key, model_cfg.model, model_cfg.max_tokens,
            model_cfg.base_url,
        )

    def build_prompt(self, docs: list[RequirementDoc], repo_slug: str) -> str:
        blob = "\n\n".join(d.as_prompt_block() for d in docs)
        if len(blob) > MAX_REQ_CHARS:
            blob = blob[:MAX_REQ_CHARS] + "\n<!-- truncated -->"
        return (
            f"Repository: {repo_slug}\n\n"
            f"Requirement documents:\n{blob}\n\n"
            f"Generate exactly {self.num_cases} test cases and the pytest module."
        )

    def generate(self, docs: list[RequirementDoc], repo_slug: str):
        payload = self.provider.generate(
            system=SYSTEM,
            prompt=self.build_prompt(docs, repo_slug),
            schema=CASE_SCHEMA,
        )
        return self.parse(payload)

    def parse(self, payload: dict):
        if not isinstance(payload, dict) or "cases" not in payload or "pytest_module" not in payload:
            raise RuntimeError(
                "Provider payload missing required keys 'cases'/'pytest_module': "
                f"got {sorted(payload) if isinstance(payload, dict) else type(payload).__name__}"
            )
        cases = [
            TestCase(
                key=_slug(c["key"]),
                title=c["title"].strip(),
                requirement_source=c.get("requirement_source", "").strip(),
                preconditions=c.get("preconditions", "").strip(),
                steps=[Step(s["step"].strip(), s["expected"].strip()) for s in c.get("steps", [])],
                priority=c.get("priority", "Medium"),
            )
            for c in payload["cases"]
        ]
        code = _strip_fences(payload["pytest_module"])

        _validate(cases, code, self.num_cases)
        log.info("Generated %d test cases", len(cases))
        return cases, code, payload.get("notes", "")


def _slug(s: str) -> str:
    s = re.sub(r"[^0-9a-zA-Z_]+", "_", s.strip().lower()).strip("_")
    if not s:
        s = "case"
    if s[0].isdigit():
        s = "c_" + s
    return s[:40]


def _strip_fences(code: str) -> str:
    code = code.strip()
    if code.startswith("```"):
        code = re.sub(r"^```[a-zA-Z]*\n", "", code)
        code = re.sub(r"\n```$", "", code)
    return code.strip() + "\n"


def _validate(cases, code: str, expected_n: int) -> None:
    if len(cases) != expected_n:
        # Warn rather than fail — local models sometimes return a slightly different count.
        log.warning("Expected %d cases, model returned %d; continuing with what was received",
                    expected_n, len(cases))
    if not cases:
        raise RuntimeError("Model returned zero test cases")
    keys = [c.key for c in cases]
    if len(set(keys)) != len(keys):
        raise RuntimeError(f"Duplicate case keys: {keys}")
    compile(code, "generated_tests.py", "exec")  # syntax check
    # Find all test function names actually defined in the code.
    defined_fns = set(re.findall(r"\bdef (test_\w+)\s*\(", code))
    missing_keys = set()
    for c in cases:
        expected_fn = f"test_{c.key}"
        if expected_fn not in defined_fns:
            missing_keys.add(c.key)
    if missing_keys:
        log.warning("Dropping %d case(s) with no matching test function: %s",
                    len(missing_keys), sorted(missing_keys))
        cases[:] = [c for c in cases if c.key not in missing_keys]
        keys = [c.key for c in cases]
    if not cases:
        raise RuntimeError("No cases have matching test functions in the generated pytest module")
    for c in cases:
        if not c.steps:
            raise RuntimeError(f"Case '{c.key}' has no steps")


def dump_cases(cases: list[TestCase]) -> str:
    return json.dumps([c.to_dict() for c in cases], indent=2)
