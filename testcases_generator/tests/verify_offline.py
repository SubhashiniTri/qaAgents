"""Stdlib-only verification harness (no pip installs required).

Stubs `requests`/`anthropic`/`dotenv` so every module imports, then exercises the pure
logic: config parsing, generator validation, JUnit parsing, TestRail payload shaping,
and Slack block building.

Run:  python3 tests/verify_offline.py
"""
from __future__ import annotations

import os
import sys
import types
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# ---------- stub third-party deps ----------
for name in ("requests", "anthropic", "openai", "dotenv"):
    if name not in sys.modules:
        sys.modules[name] = types.ModuleType(name)
sys.modules["requests"].Session = object
sys.modules["requests"].post = lambda *a, **k: None
sys.modules["dotenv"].load_dotenv = lambda *a, **k: None


# --- fake SDKs that record what they were sent and replay a canned tool call ---
CAPTURED: dict = {}


class _FakeAnthropicMessages:
    def create(self, **kw):
        CAPTURED["anthropic"] = kw
        block = types.SimpleNamespace(
            type="tool_use", name="emit_test_cases", input=FAKE_PAYLOAD)
        return types.SimpleNamespace(content=[block])


class FakeAnthropic:
    def __init__(self, api_key=None, **kw):
        self.api_key = api_key
        self.messages = _FakeAnthropicMessages()


class _FakeCompletions:
    def create(self, **kw):
        CAPTURED["openai"] = kw
        import json as _json
        call = types.SimpleNamespace(function=types.SimpleNamespace(
            name="emit_test_cases", arguments=_json.dumps(FAKE_PAYLOAD)))
        msg = types.SimpleNamespace(tool_calls=[call])
        return types.SimpleNamespace(choices=[types.SimpleNamespace(message=msg)])


class FakeOpenAI:
    def __init__(self, api_key=None, **kw):
        self.api_key = api_key
        self.chat = types.SimpleNamespace(completions=_FakeCompletions())


sys.modules["anthropic"].Anthropic = FakeAnthropic
sys.modules["openai"].OpenAI = FakeOpenAI

import config          # noqa: E402
import generator       # noqa: E402
import providers       # noqa: E402
import runner          # noqa: E402
import slack_notifier  # noqa: E402
import testrail_client as trc  # noqa: E402
from generator import Step, TestCase  # noqa: E402
from github_reader import RequirementDoc  # noqa: E402

PASS, FAIL = [], []


def check(name, fn):
    try:
        fn()
        PASS.append(name)
        print(f"  PASS  {name}")
    except Exception as e:  # noqa: BLE001
        FAIL.append((name, e))
        print(f"  FAIL  {name}: {type(e).__name__}: {e}")


def eq(a, b, msg=""):
    assert a == b, f"{msg} expected {b!r}, got {a!r}"


SAMPLE_CODE = '''
def normalize(name):
    if not isinstance(name, str):
        raise TypeError
    return name.strip().lower()

def test_happy_path(): assert normalize("  Hello ") == "hello"
def test_empty_input(): assert normalize("   ") == ""
def test_rejects_non_string(): pass
def test_deliberate_failure(): pass
def test_skipped_case(): pass
'''

CASES = [
    TestCase("happy_path", "Normalizes a padded name", "README.md", "none",
             [Step("call normalize", "lowercase trimmed")], "High"),
    TestCase("empty_input", "Handles whitespace-only input", "README.md", "none",
             [Step("call with spaces", "empty string")], "Medium"),
    TestCase("rejects_non_string", "Rejects non-string input", "README.md", "none",
             [Step("call with int", "raises TypeError")], "Critical"),
    TestCase("deliberate_failure", "Intentionally failing case", "README.md", "none",
             [Step("assert wrong value", "fails")], "Low"),
    TestCase("skipped_case", "Skipped case", "README.md", "none",
             [Step("skip", "skipped")], "Low"),
]

FAKE_PAYLOAD = {
    "cases": [
        {"key": c.key, "title": c.title, "requirement_source": c.requirement_source,
         "preconditions": c.preconditions,
         "steps": [{"step": s.step, "expected": s.expected} for s in c.steps],
         "priority": c.priority}
        for c in CASES
    ],
    "pytest_module": "```python\n" + SAMPLE_CODE + "\n```",
    "notes": "stubbed provider response",
}

JUNIT = """<?xml version="1.0" encoding="utf-8"?>
<testsuites><testsuite name="pytest" errors="0" failures="1" skipped="1" tests="5" time="0.09">
<testcase classname="test_generated" name="test_happy_path" time="0.011"/>
<testcase classname="test_generated" name="test_empty_input" time="0.004"/>
<testcase classname="test_generated" name="test_rejects_non_string" time="0.006"/>
<testcase classname="test_generated" name="test_deliberate_failure" time="0.021">
<failure message="assert 'x' == 'y'">E  AssertionError: assert 'x' == 'y'</failure></testcase>
<testcase classname="test_generated" name="test_skipped_case" time="0.001">
<skipped type="pytest.skip" message="environment not provisioned"/></testcase>
</testsuite></testsuites>
"""


# ---------------- checks ----------------

def t_slug():
    eq(generator._slug("Login — Happy Path!"), "login_happy_path")
    assert generator._slug("123abc").startswith("c_")
    assert generator._slug("").isidentifier()
    assert len(generator._slug("x" * 90)) <= 40


def t_strip_fences():
    eq(generator._strip_fences("```python\nx = 1\n```"), "x = 1\n")
    eq(generator._strip_fences("x = 1"), "x = 1\n")


def t_validate_ok():
    generator._validate(CASES, SAMPLE_CODE, 5)


def t_validate_count():
    # Count mismatch now logs a warning instead of raising.
    generator._validate(CASES[:3], SAMPLE_CODE, 5)  # should not raise


def t_validate_missing_fn():
    bad = [*CASES[:4], TestCase("not_present", "x", "y", "z", [Step("a", "b")], "Low")]
    try:
        generator._validate(bad, SAMPLE_CODE, 5)
    except RuntimeError as e:
        assert "missing test functions" in str(e)
    else:
        raise AssertionError("should have raised")


def t_validate_syntax():
    try:
        generator._validate(CASES, "def test_happy_path(: pass", 5)
    except SyntaxError:
        pass
    else:
        raise AssertionError("should have raised SyntaxError")


def t_validate_dup_keys():
    dup = [CASES[0], CASES[0], CASES[1], CASES[2], CASES[3]]
    try:
        generator._validate(dup, SAMPLE_CODE, 5)
    except RuntimeError as e:
        assert "Duplicate" in str(e)
    else:
        raise AssertionError("should have raised")


def t_junit_parse():
    with tempfile.TemporaryDirectory() as d:
        j = Path(d) / "junit.xml"
        j.write_text(JUNIT, encoding="utf-8")
        results = runner.parse_junit(j, CASES)
        by = {r.key: r.status for r in results}
        eq(by["happy_path"], "passed")
        eq(by["empty_input"], "passed")
        eq(by["rejects_non_string"], "passed")
        eq(by["deliberate_failure"], "failed")
        eq(by["skipped_case"], "skipped")
        msg = next(r.message for r in results if r.key == "deliberate_failure")
        assert "assert" in msg
        eq(runner.summarize(results),
           {"passed": 3, "failed": 1, "error": 0, "skipped": 1,
            "untested": 0, "total": 5, "ok": False})


def t_junit_missing_file():
    with tempfile.TemporaryDirectory() as d:
        results = runner.parse_junit(Path(d) / "nope.xml", CASES[:1])
        eq(results[0].status, "untested")
        eq(runner.summarize(results)["ok"], False)


def t_junit_all_pass_ok_flag():
    xml = ('<testsuites><testsuite tests="1">'
           '<testcase name="test_happy_path" time="0.01"/></testsuite></testsuites>')
    with tempfile.TemporaryDirectory() as d:
        j = Path(d) / "junit.xml"
        j.write_text(xml, encoding="utf-8")
        results = runner.parse_junit(j, CASES[:1])
        eq(runner.summarize(results)["ok"], True)


def t_junit_parametrized_name():
    xml = ('<testsuites><testsuite tests="1">'
           '<testcase name="test_happy_path[case-1]" time="0.01"/></testsuite></testsuites>')
    with tempfile.TemporaryDirectory() as d:
        j = Path(d) / "junit.xml"
        j.write_text(xml, encoding="utf-8")
        eq(runner.parse_junit(j, CASES[:1])[0].status, "passed")


def t_write_module():
    with tempfile.TemporaryDirectory() as d:
        p = runner.write_test_module(d, SAMPLE_CODE)
        assert p.exists() and p.name == "test_generated.py"
        assert "def test_happy_path" in p.read_text(encoding="utf-8")


def t_status_mapping():
    import importlib
    agent = importlib.import_module("agent")
    m = agent.STATUS_TO_TESTRAIL
    eq(m["passed"], trc.STATUS_PASSED)
    eq(m["failed"], trc.STATUS_FAILED)
    eq(m["error"], trc.STATUS_FAILED)
    eq(m["skipped"], trc.STATUS_BLOCKED)
    eq(m["untested"], trc.STATUS_UNTESTED)


def t_priority_map():
    eq(sorted(trc.PRIORITY_MAP), ["Critical", "High", "Low", "Medium"])
    for c in CASES:
        assert c.priority in trc.PRIORITY_MAP, c.priority


def t_slack_blocks():
    cases = [TestCase(c.key, c.title, c.requirement_source, c.preconditions, c.steps, c.priority)
             for c in CASES]
    for i, c in enumerate(cases, start=100):
        c.testrail_case_id = i
    with tempfile.TemporaryDirectory() as d:
        j = Path(d) / "junit.xml"
        j.write_text(JUNIT, encoding="utf-8")
        results = runner.parse_junit(j, cases)
    summary = runner.summarize(results)
    blocks, fallback = slack_notifier.build_blocks(
        repo_slug="your-org/your-repo@main", cases=cases, results=results,
        summary=summary, run_url="https://x.testrail.io/index.php?/runs/view/9",
        run_name="test run")
    assert "3/5 passed" in fallback, fallback
    assert ":red_circle:" in fallback
    eq(blocks[0]["type"], "header")
    assert len(blocks[0]["text"]["text"]) <= 150
    assert any(b.get("type") == "actions" for b in blocks)
    body = blocks[4]["text"]["text"]
    assert "Normalizes a padded name" in body and "(C100)" in body
    assert ":x:" in body and ":white_check_mark:" in body
    assert len(body) <= 3000
    import json
    json.dumps(blocks)  # must be JSON-serializable


def t_slack_green_on_all_pass():
    cases = [TestCase(CASES[0].key, CASES[0].title, "r", "", CASES[0].steps, "High")]
    results = [runner.CaseResult("happy_path", "passed", 0.1, "ok")]
    _, fallback = slack_notifier.build_blocks(
        repo_slug="r", cases=cases, results=results,
        summary=runner.summarize(results), run_url="", run_name="n")
    assert ":large_green_circle:" in fallback


def t_config_env():
    env = {
        "GITHUB_TOKEN": "t", "GITHUB_OWNER": "your-org", "GITHUB_REPO": "your-repo",
        "GITHUB_ISSUE_LABEL": "requirement",
        "TESTRAIL_URL": "https://x.testrail.io/", "TESTRAIL_USER": "u",
        "TESTRAIL_API_KEY": "k", "TESTRAIL_PROJECT_ID": "3", "TESTRAIL_SECTION_ID": "12",
        "SLACK_WEBHOOK_URL": "https://hooks.slack.com/x",
        "ANTHROPIC_API_KEY": "sk", "GITHUB_REQ_PATHS": "README.md, docs/ ,spec/",
    }
    old = dict(os.environ)
    os.environ.clear()
    os.environ.update(env)
    try:
        cfg = config.Config.from_env()
        eq(cfg.testrail.base_url, "https://x.testrail.io")
        eq(cfg.testrail.suite_id, None)
        eq(cfg.testrail.project_id, 3)
        eq(cfg.num_cases, 5)
        eq(cfg.github.paths, ["README.md", "docs/", "spec/"])
        eq(cfg.github.ref, "main")
        os.environ.pop("TESTRAIL_API_KEY")
        try:
            config.Config.from_env()
        except config.ConfigError as e:
            assert "TESTRAIL_API_KEY" in str(e)
        else:
            raise AssertionError("missing var should raise ConfigError")
    finally:
        os.environ.clear()
        os.environ.update(old)


def _model_cfg(provider):
    key_var, model_var, default_model = config.PROVIDER_ENV[provider]
    return config.ModelConfig(provider=provider, api_key="test-key",
                              model=default_model, max_tokens=1234)


def t_provider_registry():
    eq(sorted(providers.PROVIDERS), ["anthropic", "openai"])
    try:
        providers.build_provider("gemini", "k", "m")
    except providers.ProviderError as e:
        assert "Unknown MODEL_PROVIDER" in str(e)
    else:
        raise AssertionError("should reject unknown provider")


def t_provider_requires_key():
    try:
        providers.build_provider("openai", "", "gpt-4o")
    except providers.ProviderError as e:
        assert "missing API key" in str(e)
    else:
        raise AssertionError("should reject empty key")


def t_provider_name_case_insensitive():
    p = providers.build_provider("  AnThRoPiC ", "k", "m")
    eq(p.name, "anthropic")


def t_anthropic_path_end_to_end():
    CAPTURED.clear()
    gen = generator.TestCaseGenerator(_model_cfg("anthropic"), num_cases=5)
    docs = [RequirementDoc("README.md", "README", "REQ-1 normalize names")]
    cases, code, notes = gen.generate(docs, "your-org/your-repo@main")
    eq(len(cases), 5)
    eq(notes, "stubbed provider response")
    assert "def test_happy_path" in code
    assert not code.startswith("```"), "code fences must be stripped"
    sent = CAPTURED["anthropic"]
    # Anthropic envelope: input_schema + tool_choice type "tool"
    eq(sent["tools"][0]["input_schema"]["type"], "object")
    eq(sent["tool_choice"], {"type": "tool", "name": "emit_test_cases"})
    eq(sent["max_tokens"], 1234)
    assert "REQ-1 normalize names" in sent["messages"][0]["content"]
    assert sent["system"].startswith("You are a senior QA engineer")


def t_openai_path_end_to_end():
    CAPTURED.clear()
    gen = generator.TestCaseGenerator(_model_cfg("openai"), num_cases=5)
    docs = [RequirementDoc("README.md", "README", "REQ-1 normalize names")]
    cases, code, notes = gen.generate(docs, "your-org/your-repo@main")
    eq(len(cases), 5)
    assert "def test_happy_path" in code
    sent = CAPTURED["openai"]
    # OpenAI envelope: function wrapper + parameters, system as a message
    fn = sent["tools"][0]["function"]
    eq(sent["tools"][0]["type"], "function")
    eq(fn["name"], "emit_test_cases")
    eq(fn["parameters"]["type"], "object")
    eq(sent["tool_choice"], {"type": "function", "function": {"name": "emit_test_cases"}})
    eq(sent["max_completion_tokens"], 1234)
    eq(sent["messages"][0]["role"], "system")
    assert "REQ-1 normalize names" in sent["messages"][1]["content"]


def t_both_providers_agree():
    """The whole point of the abstraction: identical cases regardless of provider."""
    docs = [RequirementDoc("README.md", "README", "REQ-1")]
    a = generator.TestCaseGenerator(_model_cfg("anthropic"), 5).generate(docs, "r")
    o = generator.TestCaseGenerator(_model_cfg("openai"), 5).generate(docs, "r")
    eq([c.to_dict() for c in a[0]], [c.to_dict() for c in o[0]])
    eq(a[1], o[1])


def t_schema_shared_not_provider_specific():
    assert "input_schema" not in generator.CASE_SCHEMA
    assert "parameters" not in generator.CASE_SCHEMA
    eq(generator.CASE_SCHEMA["required"], ["cases", "pytest_module"])
    props = generator.CASE_SCHEMA["properties"]["cases"]["items"]["properties"]
    eq(sorted(props), ["key", "preconditions", "priority", "requirement_source", "steps", "title"])
    eq(props["priority"]["enum"], ["Low", "Medium", "High", "Critical"])


def t_parse_rejects_malformed_payload():
    gen = generator.TestCaseGenerator(_model_cfg("anthropic"), 5)
    for bad in ({}, {"cases": []}, {"pytest_module": "x"}, "not a dict"):
        try:
            gen.parse(bad)
        except RuntimeError as e:
            assert "missing required keys" in str(e)
        else:
            raise AssertionError(f"should have rejected {bad!r}")


def t_openai_bad_json_arguments():
    p = providers.build_provider("openai", "k", "gpt-4o")
    call = types.SimpleNamespace(function=types.SimpleNamespace(
        name="emit_test_cases", arguments="{not json"))
    msg = types.SimpleNamespace(tool_calls=[call])

    class C:
        def create(self, **kw):
            return types.SimpleNamespace(choices=[types.SimpleNamespace(message=msg)])

    sys.modules["openai"].OpenAI = lambda api_key=None, **kw: types.SimpleNamespace(
        chat=types.SimpleNamespace(completions=C()))
    try:
        p.generate(system="s", prompt="p", schema={})
    except providers.ProviderError as e:
        assert "not valid JSON" in str(e)
    else:
        raise AssertionError("should have raised")
    finally:
        sys.modules["openai"].OpenAI = FakeOpenAI


def t_missing_tool_call_raises():
    class C:
        def create(self, **kw):
            msg = types.SimpleNamespace(tool_calls=None)
            return types.SimpleNamespace(choices=[types.SimpleNamespace(message=msg)])

    sys.modules["openai"].OpenAI = lambda api_key=None, **kw: types.SimpleNamespace(
        chat=types.SimpleNamespace(completions=C()))
    try:
        providers.build_provider("openai", "k", "m").generate(system="s", prompt="p", schema={})
    except providers.ProviderError as e:
        assert "did not return" in str(e)
    else:
        raise AssertionError("should have raised")
    finally:
        sys.modules["openai"].OpenAI = FakeOpenAI


def t_config_provider_selection():
    base = {
        "GITHUB_TOKEN": "t", "GITHUB_OWNER": "o",
        "TESTRAIL_URL": "https://x.testrail.io", "TESTRAIL_USER": "u",
        "TESTRAIL_API_KEY": "k", "TESTRAIL_PROJECT_ID": "1", "TESTRAIL_SECTION_ID": "2",
    }
    old = dict(os.environ)
    try:
        # openai selected -> only OPENAI_API_KEY needed, anthropic key absent
        os.environ.clear()
        os.environ.update({**base, "MODEL_PROVIDER": "openai", "OPENAI_API_KEY": "sk-proj-x"})
        cfg = config.Config.from_env()
        eq(cfg.model_cfg.provider, "openai")
        eq(cfg.model_cfg.model, "gpt-4o")
        eq(cfg.model_cfg.api_key, "sk-proj-x")

        # default provider is anthropic
        os.environ.clear()
        os.environ.update({**base, "ANTHROPIC_API_KEY": "sk-ant-x"})
        eq(config.Config.from_env().model_cfg.provider, "anthropic")

        # wrong key for selected provider -> actionable error
        os.environ.clear()
        os.environ.update({**base, "MODEL_PROVIDER": "openai", "ANTHROPIC_API_KEY": "sk-ant-x"})
        try:
            config.Config.from_env()
        except config.ConfigError as e:
            assert "OPENAI_API_KEY" in str(e) and "subscription does not include" in str(e)
        else:
            raise AssertionError("should have raised")

        # unknown provider rejected
        os.environ.clear()
        os.environ.update({**base, "MODEL_PROVIDER": "gemini", "ANTHROPIC_API_KEY": "x"})
        try:
            config.Config.from_env()
        except config.ConfigError as e:
            assert "MODEL_PROVIDER must be one of" in str(e)
        else:
            raise AssertionError("should have raised")

        # explicit model override honoured
        os.environ.clear()
        os.environ.update({**base, "MODEL_PROVIDER": "openai", "OPENAI_API_KEY": "k",
                           "OPENAI_MODEL": "gpt-4.1-mini", "MODEL_MAX_TOKENS": "8000"})
        cfg = config.Config.from_env()
        eq(cfg.model_cfg.model, "gpt-4.1-mini")
        eq(cfg.model_cfg.max_tokens, 8000)
    finally:
        os.environ.clear()
        os.environ.update(old)


def t_all_modules_compile():
    import py_compile
    for f in sorted(ROOT.glob("*.py")) + sorted((ROOT / "tests").glob("*.py")):
        py_compile.compile(str(f), doraise=True)


if __name__ == "__main__":
    print("Testcase Agent — offline verification\n")
    for n, f in [
        ("all modules compile", t_all_modules_compile),
        ("config: env parsing + required-var errors", t_config_env),
        ("config: provider selection + per-provider key rules", t_config_provider_selection),
        ("providers: registry + unknown provider rejected", t_provider_registry),
        ("providers: empty API key rejected", t_provider_requires_key),
        ("providers: name is case/space insensitive", t_provider_name_case_insensitive),
        ("providers: schema is shared, not provider-specific", t_schema_shared_not_provider_specific),
        ("providers: anthropic envelope + end-to-end parse", t_anthropic_path_end_to_end),
        ("providers: openai envelope + end-to-end parse", t_openai_path_end_to_end),
        ("providers: both providers yield identical cases", t_both_providers_agree),
        ("providers: malformed payload rejected", t_parse_rejects_malformed_payload),
        ("providers: openai invalid JSON args rejected", t_openai_bad_json_arguments),
        ("providers: absent tool call rejected", t_missing_tool_call_raises),
        ("generator: slug sanitization", t_slug),
        ("generator: code fence stripping", t_strip_fences),
        ("generator: validation accepts good payload", t_validate_ok),
        ("generator: rejects wrong case count", t_validate_count),
        ("generator: rejects missing test function", t_validate_missing_fn),
        ("generator: rejects syntax-broken code", t_validate_syntax),
        ("generator: rejects duplicate keys", t_validate_dup_keys),
        ("runner: writes test module", t_write_module),
        ("runner: parses JUnit pass/fail/skip", t_junit_parse),
        ("runner: parametrized test names map to case", t_junit_parametrized_name),
        ("runner: missing JUnit -> untested", t_junit_missing_file),
        ("runner: all-pass sets ok flag", t_junit_all_pass_ok_flag),
        ("agent: status mapping to TestRail ids", t_status_mapping),
        ("testrail: priority map covers all priorities", t_priority_map),
        ("slack: block payload well-formed", t_slack_blocks),
        ("slack: green header when all pass", t_slack_green_on_all_pass),
    ]:
        check(n, f)

    print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
    sys.exit(1 if FAIL else 0)
