# Testcase Agent

Reads requirements from a GitHub repo, generates 5 test cases with Claude, uploads
them to TestRail, executes the generated tests, pushes results back to TestRail, and posts a
status summary to Slack.

```
GitHub (docs + issues)
        ↓  github_reader.py
Claude *or* GPT (structured tool call → cases + pytest module)
        ↓  generator.py + providers.py
TestRail add_case ×5  →  add_run
        ↓  testrail_client.py
pytest execution → JUnit XML
        ↓  runner.py
TestRail add_results_for_cases
        ↓
Slack Block Kit message
        ↓  slack_notifier.py
```

## Setup

```bash
pip install -r requirements.txt
cp .env.example .env     # fill in tokens and IDs
```

| Variable | Where to get it |
|---|---|
| `GITHUB_TOKEN` | GitHub → Settings → Developer settings → PAT, scope `repo` (read) |
| `GITHUB_OWNER` / `GITHUB_REPO` | org and repo name, e.g. `your-org` / `your-repo` |
| `GITHUB_REQ_PATHS` | comma-separated files/dirs scanned for requirements |
| `GITHUB_ISSUE_LABEL` | optional — also pull open issues with this label |
| `MODEL_PROVIDER` | `anthropic` (default) or `openai` |
| `ANTHROPIC_API_KEY` / `ANTHROPIC_MODEL` | console.anthropic.com — required when provider is `anthropic` |
| `OPENAI_API_KEY` / `OPENAI_MODEL` | platform.openai.com — required when provider is `openai` |
| `TESTRAIL_URL` | `https://yourorg.testrail.io` |
| `TESTRAIL_USER` / `TESTRAIL_API_KEY` | TestRail → My Settings → API Keys (API must be enabled under Site Settings) |
| `TESTRAIL_PROJECT_ID` / `TESTRAIL_SECTION_ID` | from the TestRail URL when viewing the project/section |
| `TESTRAIL_SUITE_ID` | only for multi-suite projects |
| `SLACK_WEBHOOK_URL` | Slack app → Incoming Webhooks (simplest) |
| `SLACK_BOT_TOKEN` + `SLACK_CHANNEL` | alternative — bot token needs `chat:write` |

### Choosing a model provider

Only the selected provider's key is required — you don't need both.

```ini
MODEL_PROVIDER=anthropic
ANTHROPIC_API_KEY=sk-ant-...
```
```ini
MODEL_PROVIDER=openai
OPENAI_API_KEY=sk-proj-...
OPENAI_MODEL=gpt-4o          # set to a model your account can access
```

**A Claude app or ChatGPT subscription does not include API access.** Those cover the chat
apps only. API keys come from the developer consoles (console.anthropic.com,
platform.openai.com) and are billed per token against their own credit balance.

Adding a third provider means one subclass in `providers.py` implementing
`generate(system, prompt, schema) -> dict` and one entry in the `PROVIDERS` registry.
Nothing else in the pipeline changes.

## Run

```bash
python agent.py                    # full pipeline
python agent.py --dry-run          # generate + execute only; no TestRail writes, no Slack
python agent.py --skip-testrail    # execute + Slack, no TestRail
python agent.py --close-run        # close the TestRail run at the end
python agent.py --from-fixture fixtures/sample_requirements.md   # offline requirements
```

Exit code is `0` when every case passed, `1` otherwise — usable as a CI gate.

## Artifacts

Written to `AGENT_WORKDIR` (default `./qa_run`):

- `test_cases.json` — structured cases with TestRail IDs
- `test_generated.py` — the executable test module
- `junit.xml`, `pytest_output.txt` — raw execution results
- `slack_payload.json` — the exact Block Kit payload sent

## Verify

```bash
python3 tests/verify_offline.py    # stdlib only, no installs, no network — 29 checks
python -m pytest tests/ -q         # fuller suite, needs pytest installed
```

## Design notes

- **Provider-agnostic by design.** `providers.py` is the only module that touches a model
  SDK. It wraps one shared JSON Schema (`generator.CASE_SCHEMA`) in whichever envelope the
  SDK wants — Anthropic's `input_schema`, OpenAI's `function.parameters` — and returns the
  same dict either way. Everything downstream sees only `TestCase` objects.
- **Generation is constrained, not freeform.** The model must answer through the
  `emit_test_cases` tool schema, and `generator._validate` rejects the response if the count
  is wrong, keys collide, the code has a syntax error, or a `test_<key>` function is missing.
  A bad generation fails fast rather than silently uploading junk to TestRail.
- **Cases are matched to results by `key`**, carried as the pytest function name
  (`test_<key>`) and stored on the TestRail case as `custom_automation_key`. Cases with no
  matching pytest result are reported as `untested`, never silently dropped.
- **Status mapping:** passed→1, blocked/skipped→2, untested→3, failed & error→5.
- **The generated code is executed.** It runs in a subprocess with a 600s timeout, but it is
  model-written code — run this in a container or CI runner, not on a developer laptop with
  production credentials in the environment.
- Each run creates *new* TestRail cases. To update existing cases instead, swap `add_case`
  for `update_case` keyed on `custom_automation_key`.
