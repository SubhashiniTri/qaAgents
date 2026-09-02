"""Offline smoke tests: exercise generator validation, runner, and Slack payload building
without touching GitHub, Anthropic, TestRail, or Slack."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import generator  # noqa: E402
import runner  # noqa: E402
import slack_notifier  # noqa: E402
from generator import Step, TestCase  # noqa: E402

SAMPLE_CODE = '''
"""Requirement-contract tests."""

def normalize(name):
    if not isinstance(name, str):
        raise TypeError("name must be str")
    return name.strip().lower()


def test_happy_path():
    assert normalize("  Hello ") == "hello"


def test_empty_input():
    assert normalize("   ") == ""


def test_rejects_non_string():
    import pytest
    with pytest.raises(TypeError):
        normalize(42)


def test_deliberate_failure():
    assert normalize("x") == "y"


def test_skipped_case():
    import pytest
    pytest.skip("environment not provisioned")
'''

CASES = [
    TestCase("happy_path", "Normalizes a padded name", "README.md", "none",
             [Step("call normalize", "returns lowercase trimmed")], "High"),
    TestCase("empty_input", "Handles whitespace-only input", "README.md", "none",
             [Step("call normalize with spaces", "returns empty string")], "Medium"),
    TestCase("rejects_non_string", "Rejects non-string input", "README.md", "none",
             [Step("call normalize with int", "raises TypeError")], "High"),
    TestCase("deliberate_failure", "Intentionally failing case", "README.md", "none",
             [Step("assert wrong value", "fails")], "Low"),
    TestCase("skipped_case", "Skipped case", "README.md", "none",
             [Step("skip", "skipped")], "Low"),
]


def test_generator_validation_accepts_good_payload():
    generator._validate(CASES, SAMPLE_CODE, 5)


def test_generator_validation_rejects_missing_function():
    bad = [*CASES[:4], TestCase("not_present", "x", "y", "z", [Step("a", "b")], "Low")]
    with pytest.raises(RuntimeError, match="missing test functions"):
        generator._validate(bad, SAMPLE_CODE, 5)


def test_generator_validation_rejects_wrong_count():
    # Count mismatch logs a warning but does not raise.
    generator._validate(CASES[:3], SAMPLE_CODE, 5)  # should not raise


def test_slug_sanitizes():
    assert generator._slug("Login — Happy Path!") == "login_happy_path"
    assert generator._slug("123abc").startswith("c_")


def test_strip_fences():
    assert generator._strip_fences("```python\nx = 1\n```") == "x = 1\n"


def test_end_to_end_run_and_parse(tmp_path):
    cases = [TestCase(c.key, c.title, c.requirement_source, c.preconditions, c.steps, c.priority)
             for c in CASES]
    path = runner.write_test_module(str(tmp_path), SAMPLE_CODE)
    rc, output, junit = runner.run_pytest(str(tmp_path), path)
    assert junit.exists(), output

    results = runner.parse_junit(junit, cases)
    by_key = {r.key: r.status for r in results}
    assert by_key["happy_path"] == "passed"
    assert by_key["empty_input"] == "passed"
    assert by_key["rejects_non_string"] == "passed"
    assert by_key["deliberate_failure"] == "failed"
    assert by_key["skipped_case"] == "skipped"

    summary = runner.summarize(results)
    assert summary == {"passed": 3, "failed": 1, "error": 0, "skipped": 1,
                       "untested": 0, "total": 5, "ok": False}


def test_untested_when_no_pytest_result(tmp_path):
    orphan = [TestCase("never_ran", "Orphan", "req", "", [Step("a", "b")], "Low")]
    results = runner.parse_junit(tmp_path / "missing.xml", orphan)
    assert results[0].status == "untested"


def test_slack_blocks_are_wellformed(tmp_path):
    cases = [TestCase(c.key, c.title, c.requirement_source, c.preconditions, c.steps, c.priority)
             for c in CASES]
    for i, c in enumerate(cases, start=100):
        c.testrail_case_id = i
    path = runner.write_test_module(str(tmp_path), SAMPLE_CODE)
    runner.run_pytest(str(tmp_path), path)
    results = runner.parse_junit(tmp_path / "junit.xml", cases)
    summary = runner.summarize(results)

    blocks, fallback = slack_notifier.build_blocks(
        repo_slug="your-org/your-repo@main", cases=cases, results=results,
        summary=summary, run_url="https://x.testrail.io/index.php?/runs/view/9",
        run_name="test run",
    )
    assert "3/5 passed" in fallback
    assert blocks[0]["type"] == "header"
    assert any(b.get("type") == "actions" for b in blocks)
    body = blocks[4]["text"]["text"]
    assert "Normalizes a padded name" in body
    assert "(C100)" in body
    assert len(body) <= 3000
