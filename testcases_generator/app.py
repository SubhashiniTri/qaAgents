"""Streamlit UI for the Testcase Agent — friendly interface for non-technical QA."""
from __future__ import annotations

import json
import logging
import os
import sys
from datetime import datetime, timezone
from io import StringIO
from pathlib import Path

import streamlit as st

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

try:
    from dotenv import dotenv_values
    _env_defaults: dict = dotenv_values(ROOT / ".env")
except ImportError:
    _env_defaults = {}


def _env(key: str, default: str = "") -> str:
    """Return UI-friendly default: .env first, then os.environ, then fallback."""
    raw = _env_defaults.get(key) or os.environ.get(key, default) or default
    return raw.split("#")[0].strip()


# ── page config ──────────────────────────────────────────────────────────────
st.set_page_config(page_title="Testcase Agent", page_icon="🧪", layout="wide")

st.title("🧪 Testcase Agent")
st.caption("Generate, review, and track test cases from GitHub requirements — no code required.")

# ── session-state defaults ────────────────────────────────────────────────────
for key, val in {
    "cases": None,      # list[TestCase] after generation
    "code": None,       # pytest module string
    "notes": "",        # generator notes
    "approved": None,   # list[bool] per case
    "results": None,    # list[CaseResult]
    "summary": None,    # summarize() dict
    "run_url": "",
    "logs": [],
}.items():
    if key not in st.session_state:
        st.session_state[key] = val


def _log(msg: str) -> None:
    st.session_state.logs.append(msg)


# ── sidebar: configuration ────────────────────────────────────────────────────
with st.sidebar:
    st.header("⚙️ Configuration")

    with st.expander("🔗 GitHub", expanded=True):
        gh_token  = st.text_input("Personal Access Token", value=_env("GITHUB_TOKEN"),
                                  type="password", help="PAT with repo:read scope")
        gh_owner  = st.text_input("Organisation / Owner", value=_env("GITHUB_OWNER"),
                                  placeholder="e.g. your-org")
        gh_repo   = st.text_input("Repository", value=_env("GITHUB_REPO"),
                                  placeholder="e.g. your-repo")
        gh_ref    = st.text_input("Branch / Tag", value=_env("GITHUB_REF", "main"))
        gh_paths  = st.text_input("Requirement paths (comma-separated)",
                                  value=_env("GITHUB_REQ_PATHS", "README.md,docs/"),
                                  help="Files or folders scanned for requirements")
        gh_label  = st.text_input("Issue label (optional)",
                                  value=_env("GITHUB_ISSUE_LABEL"),
                                  placeholder="e.g. requirement")

    with st.expander("🤖 AI Model", expanded=True):
        provider  = st.selectbox("Provider", ["anthropic", "openai", "ollama"],
                                 index=["anthropic", "openai", "ollama"].index(
                                     _env("MODEL_PROVIDER", "anthropic")))
        api_key   = st.text_input("API Key", value=_env(
                                      "ANTHROPIC_API_KEY" if provider == "anthropic" else
                                      "OPENAI_API_KEY"    if provider == "openai" else ""),
                                  type="password",
                                  help="Not required for Ollama" if provider == "ollama" else "")
        model_name = st.text_input("Model", value=_env(
                                      "ANTHROPIC_MODEL" if provider == "anthropic" else
                                      "OPENAI_MODEL"    if provider == "openai" else
                                      "OLLAMA_MODEL",
                                      {"anthropic": "claude-sonnet-5",
                                       "openai": "gpt-4o",
                                       "ollama": "qwen2.5"}[provider]))
        base_url  = st.text_input("Base URL",
                                  value=_env("OLLAMA_BASE_URL", "http://localhost:11434/v1")
                                        if provider == "ollama" else _env("OPENAI_BASE_URL"),
                                  help="Ollama default: http://localhost:11434/v1" if provider == "ollama"
                                       else "Leave blank unless using a custom endpoint")
        max_tok   = st.number_input("Max tokens", value=int(_env("MODEL_MAX_TOKENS", "16000")),
                                    min_value=1000, max_value=64000, step=1000)
        num_cases = st.number_input("Number of test cases", value=int(_env("NUM_TEST_CASES", "5")),
                                    min_value=1, max_value=20)
    with st.expander("🐛 Jira"):
        jira_url     = st.text_input("Jira Base URL", value=_env("JIRA_BASE_URL"),
                                     placeholder="https://yourorg.atlassian.net")
        jira_user    = st.text_input("Jira User (email)", value=_env("JIRA_USER"))
        jira_token   = st.text_input("Jira API Token", value=_env("JIRA_API_TOKEN"),
                                     type="password",
                                     help="Atlassian account → Security → API tokens")
        jira_version = st.selectbox("API version", ["3", "2"],
                                    index=0 if _env("JIRA_API_VERSION", "3") == "3" else 1,
                                    help="3 = Cloud, 2 = Server/Data Center")
    with st.expander("🧾 TestRail"):
        tr_url    = st.text_input("TestRail URL", value=_env("TESTRAIL_URL"),
                                  placeholder="https://yourorg.testrail.io")
        tr_user   = st.text_input("TestRail User (email)", value=_env("TESTRAIL_USER"))
        tr_key    = st.text_input("TestRail API Key", value=_env("TESTRAIL_API_KEY"),
                                  type="password")
        tr_proj   = st.number_input("Project ID", value=int(_env("TESTRAIL_PROJECT_ID") or 0),
                                    min_value=0)
        tr_suite  = st.number_input("Suite ID (0 = single-suite)",
                                    value=int(_env("TESTRAIL_SUITE_ID") or 0), min_value=0)
        tr_sect   = st.number_input("Section ID", value=int(_env("TESTRAIL_SECTION_ID") or 0),
                                    min_value=0)

    with st.expander("💬 Slack"):
        sl_webhook = st.text_input("Webhook URL", value=_env("SLACK_WEBHOOK_URL"),
                                   type="password", placeholder="https://hooks.slack.com/…")
        sl_token   = st.text_input("Bot Token (alternative)", value=_env("SLACK_BOT_TOKEN"),
                                   type="password", placeholder="xoxb-…")
        sl_channel = st.text_input("Channel (required with bot token)",
                                   value=_env("SLACK_CHANNEL"), placeholder="#qa-automation")

    with st.expander("🔧 Run options"):
        dry_run       = st.checkbox("Dry run (no TestRail writes, no Slack)", value=False)
        skip_testrail = st.checkbox("Skip TestRail", value=False)
        skip_slack    = st.checkbox("Skip Slack", value=False)
        close_run     = st.checkbox("Close TestRail run after execution", value=False)


# ── helper: build Config from sidebar values ──────────────────────────────────
def _build_config() -> object:
    from config import (Config, GitHubConfig, TestRailConfig,
                        SlackConfig, ModelConfig, JiraConfig)
    paths = [p.strip() for p in gh_paths.split(",") if p.strip()] or ["README.md"]
    return Config(
        github=GitHubConfig(
            token=gh_token, owner=gh_owner, repo=gh_repo, ref=gh_ref,
            paths=paths, issue_label=gh_label,
        ),
        testrail=TestRailConfig(
            base_url=tr_url.rstrip("/"), user=tr_user, api_key=tr_key,
            project_id=int(tr_proj), suite_id=int(tr_suite) or None,
            section_id=int(tr_sect),
        ),
        slack=SlackConfig(webhook_url=sl_webhook, bot_token=sl_token, channel=sl_channel),
        jira=JiraConfig(
            base_url=jira_url, user=jira_user, api_token=jira_token,
            api_version=jira_version,
        ),
        model_cfg=ModelConfig(
            provider=provider, api_key=api_key, model=model_name,
            max_tokens=int(max_tok), base_url=base_url,
        ),
        num_cases=int(num_cases),
        workdir=str(ROOT / "qa_run"),
    )


# ── STEP 1 — source input ─────────────────────────────────────────────────────
st.subheader("Step 1 — Choose requirement source(s)")
st.caption("Select one or more sources. Requirements from all selected sources are combined.")

col_src = st.columns(5)
use_gh_files  = col_src[0].checkbox("GitHub repo files")
use_gh_issue  = col_src[1].checkbox("GitHub issue #")
use_jira_key  = col_src[2].checkbox("Jira issue key")
use_jira_jql  = col_src[3].checkbox("Jira JQL query")
use_fixture   = col_src[4].checkbox("Upload fixture file")

gh_issue_number: int | None = None
jira_issue_key: str | None = None
jira_jql_query: str | None = None
fixture_text: str | None = None
fixture_name = "fixture"

if use_gh_files:
    st.info(f"📂 Will scan **{gh_owner}/{gh_repo}** paths: `{gh_paths}`")

if use_gh_issue:
    gh_issue_number = st.number_input("GitHub issue number", min_value=1, step=1, value=1,
                                      key="gh_issue_num")

if use_jira_key:
    raw_key = st.text_input("Jira issue key", placeholder="PROJ-123", key="jira_key")
    jira_issue_key = raw_key.strip() or None

if use_jira_jql:
    raw_jql = st.text_area("Jira JQL query",
                           placeholder='project = PROJ AND issuetype = Story AND sprint in openSprints()',
                           key="jira_jql")
    jira_jql_query = raw_jql.strip() or None

if use_fixture:
    uploaded = st.file_uploader("Upload a Markdown / text requirements file",
                                type=["md", "txt", "rst", "feature"])
    if uploaded:
        fixture_text = uploaded.read().decode("utf-8")
        fixture_name = uploaded.name
        st.success(f"Loaded **{fixture_name}** ({len(fixture_text):,} characters)")

if not any([use_gh_files, use_gh_issue, use_jira_key, use_jira_jql, use_fixture]):
    st.info("👉 Select at least one source above to get started.")

# ── STEP 2 — generate ─────────────────────────────────────────────────────────
st.subheader("Step 2 — Generate test cases")

if st.button("✨ Generate Test Cases", type="primary"):
    st.session_state.cases    = None
    st.session_state.approved = None
    st.session_state.results  = None
    st.session_state.logs     = []

    if not any([use_gh_files, use_gh_issue, use_jira_key, use_jira_jql, use_fixture]):
        st.error("Select at least one requirement source above.")
        st.stop()

    # validate minimum required fields
    missing = []
    if (use_gh_files or use_gh_issue) and not gh_token:  missing.append("GitHub Token")
    if (use_gh_files or use_gh_issue) and not gh_owner:  missing.append("GitHub Owner")
    if (use_gh_files or use_gh_issue) and not gh_repo:   missing.append("GitHub Repo")
    if use_jira_key and (not jira_url or not jira_user or not jira_token):
        missing.append("Jira URL / User / Token")
    if use_jira_jql and (not jira_url or not jira_user or not jira_token):
        missing.append("Jira URL / User / Token")
    if provider != "ollama" and not api_key:  missing.append("AI API Key")
    if missing:
        st.error(f"Please fill in: {', '.join(missing)}")
        st.stop()

    with st.spinner("Fetching requirements and generating test cases…"):
        try:
            from github_reader import GitHubReader, RequirementDoc
            from jira_reader import JiraReader
            from generator import TestCaseGenerator, dump_cases

            cfg = _build_config()
            docs: list = []
            slugs: list[str] = []

            if use_fixture and fixture_text:
                docs.append(RequirementDoc(source=fixture_name, title=fixture_name, text=fixture_text))
                slugs.append(f"fixture:{fixture_name}")

            if use_gh_issue and gh_issue_number:
                reader = GitHubReader(cfg.github)
                gh_docs = reader.fetch_issue(int(gh_issue_number))
                docs.extend(gh_docs if isinstance(gh_docs, list) else [gh_docs])
                slugs.append(reader.repo_slug())

            if use_gh_files:
                reader = GitHubReader(cfg.github)
                gh_docs = reader.fetch()
                docs.extend(gh_docs)
                slugs.append(reader.repo_slug())

            if use_jira_key and jira_issue_key:
                jr = JiraReader(cfg.jira)
                docs.append(jr.fetch_issue(jira_issue_key))
                slugs.append(f"jira:{jira_issue_key}")

            if use_jira_jql and jira_jql_query:
                jr = JiraReader(cfg.jira)
                docs.extend(jr.fetch_by_jql(jira_jql_query))
                slugs.append("jira-jql")

            if not docs:
                st.error("No requirement documents found. Check your settings and source selections.")
                st.stop()

            repo_slug = " + ".join(slugs)

            gen = TestCaseGenerator(cfg.model_cfg, cfg.num_cases)
            cases, code, notes = gen.generate(docs, repo_slug)

            st.session_state.cases    = cases
            st.session_state.code     = code
            st.session_state.notes    = notes
            st.session_state.approved = [True] * len(cases)
            st.session_state.repo_slug = repo_slug

            Path(cfg.workdir).mkdir(parents=True, exist_ok=True)
            (Path(cfg.workdir) / "test_cases.json").write_text(
                dump_cases(cases), encoding="utf-8")

        except Exception as exc:
            st.error(f"Generation failed: {exc}")
            st.stop()

if st.session_state.cases:
    notes_val = st.session_state.get("notes", "")
    if notes_val:
        st.info(f"**AI notes:** {notes_val}")

# ── STEP 3 — review & approve ─────────────────────────────────────────────────
if st.session_state.cases:
    st.subheader("Step 3 — Review & approve test cases")
    st.caption("Uncheck any cases you want to exclude before uploading to TestRail.")

    cases    = st.session_state.cases
    approved = st.session_state.approved

    for i, case in enumerate(cases):
        col_check, col_body = st.columns([1, 11])
        with col_check:
            approved[i] = st.checkbox("", value=approved[i], key=f"approve_{i}")
        with col_body:
            priority_color = {"Critical": "🔴", "High": "🟠",
                              "Medium": "🟡", "Low": "🟢"}.get(case.priority, "⚪")
            with st.expander(f"{priority_color} **{case.title}** — {case.priority}", expanded=False):
                st.markdown(f"**Source:** `{case.requirement_source}`")
                if case.preconditions and case.preconditions.lower() != "none":
                    st.markdown(f"**Preconditions:** {case.preconditions}")
                for j, step in enumerate(case.steps, 1):
                    st.markdown(f"**Step {j}:** {step.step}  \n→ *{step.expected}*")

    st.session_state.approved = approved
    approved_count = sum(approved)
    st.markdown(f"**{approved_count} of {len(cases)} cases selected**")

    # ── STEP 4 — create in TestRail & run ─────────────────────────────────────
    st.subheader("Step 4 — Create in TestRail & run tests")

    if approved_count == 0:
        st.warning("Select at least one test case to proceed.")
    else:
        if st.button("🚀 Create in TestRail & Execute Tests", type="primary"):
            from testrail_client import (TestRailClient,
                STATUS_PASSED, STATUS_FAILED, STATUS_BLOCKED, STATUS_UNTESTED)
            import runner as _runner
            import slack_notifier as _slack

            STATUS_MAP = {"passed": STATUS_PASSED, "failed": STATUS_FAILED,
                          "error": STATUS_FAILED, "skipped": STATUS_BLOCKED,
                          "untested": STATUS_UNTESTED}

            selected_cases = [c for c, ok in zip(cases, approved) if ok]
            cfg        = _build_config()
            workdir    = Path(cfg.workdir)
            workdir.mkdir(parents=True, exist_ok=True)
            stamp      = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
            run_name   = f"Testcase Agent run — {stamp} ({cfg.model_cfg.model})"
            repo_slug  = st.session_state.get("repo_slug", gh_repo)

            progress = st.progress(0, text="Starting…")
            log_box  = st.empty()
            log_lines: list[str] = []

            def update_log(msg: str) -> None:
                log_lines.append(msg)
                log_box.code("\n".join(log_lines[-20:]))

            tr     = None
            run_id = None

            # 1. TestRail upload
            if not dry_run and not skip_testrail:
                if not tr_url or not tr_user or not tr_key or not tr_proj or not tr_sect:
                    st.error("Fill in all TestRail fields to upload cases.")
                    st.stop()
                try:
                    progress.progress(20, text="Creating cases in TestRail…")
                    update_log("⏳ Uploading cases to TestRail…")
                    tr       = TestRailClient(cfg.testrail)
                    case_ids = tr.upload_cases(selected_cases, run_name)
                    run_id   = tr.add_run(
                        run_name,
                        f"Auto-generated from {repo_slug} by testcase-agent.",
                        case_ids,
                    )
                    st.session_state.run_url = tr.run_url(run_id)
                    update_log(f"✅ {len(case_ids)} cases created. Run ID: {run_id}")

                    # early Slack: cases created
                    if not skip_slack:
                        try:
                            cr_blocks, cr_fallback = _slack.build_creation_blocks(
                                repo_slug=repo_slug, cases=selected_cases,
                                run_url=st.session_state.run_url, run_name=run_name)
                            _slack.send(cfg.slack, cr_blocks, cr_fallback)
                            update_log("💬 Slack creation notification sent.")
                        except Exception as e:
                            update_log(f"⚠️  Slack creation notification failed: {e}")
                except Exception as e:
                    st.error(f"TestRail upload failed: {e}")
                    st.stop()
            else:
                update_log("ℹ️  Skipping TestRail (dry-run / skip flag).")

            # 2. execute tests
            progress.progress(50, text="Running tests…")
            update_log("⏳ Executing generated tests…")
            try:
                test_path = _runner.write_test_module(str(workdir), st.session_state.code)
                rc, output, junit = _runner.run_pytest(str(workdir), test_path)
                (workdir / "pytest_output.txt").write_text(output, encoding="utf-8")
                results = _runner.parse_junit(junit, selected_cases)
                summary = _runner.summarize(results)
                st.session_state.results = results
                st.session_state.summary = summary
                update_log(f"✅ Tests finished — {summary['passed']}/{summary['total']} passed.")
            except Exception as e:
                st.error(f"Test execution failed: {e}")
                st.stop()

            # 3. push results to TestRail
            if tr and run_id:
                progress.progress(75, text="Pushing results to TestRail…")
                try:
                    payload = [
                        {"case_id": c.testrail_case_id,
                         "status_id": STATUS_MAP.get(r.status, STATUS_UNTESTED),
                         "comment": f"testcase-agent\nStatus: {r.status}\n\n{r.message}"[:8000]}
                        for c, r in zip(selected_cases, results)
                        if c.testrail_case_id
                    ]
                    tr.add_results(run_id, payload)
                    if close_run:
                        tr.close_run(run_id)
                    update_log("✅ Results pushed to TestRail.")
                except Exception as e:
                    update_log(f"⚠️  Could not push results to TestRail: {e}")

            # 4. final Slack
            if not dry_run and not skip_slack:
                progress.progress(90, text="Sending Slack summary…")
                try:
                    blocks, fallback = _slack.build_blocks(
                        repo_slug=repo_slug, cases=selected_cases, results=results,
                        summary=summary, run_url=st.session_state.run_url,
                        run_name=run_name)
                    (workdir / "slack_payload.json").write_text(
                        json.dumps({"text": fallback, "blocks": blocks}, indent=2),
                        encoding="utf-8")
                    _slack.send(cfg.slack, blocks, fallback)
                    update_log("💬 Final Slack summary sent.")
                except Exception as e:
                    update_log(f"⚠️  Final Slack notification failed: {e}")

            progress.progress(100, text="Done!")

# ── STEP 5 — results ──────────────────────────────────────────────────────────
if st.session_state.results and st.session_state.summary:
    st.subheader("Step 5 — Results")

    summary = st.session_state.summary
    results = st.session_state.results
    run_url = st.session_state.run_url

    # KPI row
    c1, c2, c3, c4, c5 = st.columns(5)
    c1.metric("✅ Passed",  summary["passed"])
    c2.metric("❌ Failed",  summary["failed"])
    c3.metric("⚠️ Errors",  summary["error"])
    c4.metric("⏭ Skipped", summary["skipped"])
    c5.metric("❓ Untested", summary["untested"])

    # verdict
    fail_count = summary["failed"] + summary["error"]
    if summary["ok"]:
        st.success("🚀 **Feature tested successfully — ready for release candidate.**")
    else:
        st.error(f"⚠️ **{fail_count} test(s) failing — not ready for release.**")

    if run_url:
        st.link_button("Open TestRail Run", run_url)

    # per-case breakdown
    st.divider()
    status_icon = {"passed": "✅", "failed": "❌", "error": "🔥",
                   "skipped": "⏭️", "untested": "❓"}
    cases_map = {c.key: c for c in (st.session_state.cases or [])}
    for r in results:
        case  = cases_map.get(r.key)
        title = case.title if case else r.key
        icon  = status_icon.get(r.status, "❓")
        cid   = f" (C{case.testrail_case_id})" if case and case.testrail_case_id else ""
        with st.expander(f"{icon} {title}{cid} — {r.status.upper()}", expanded=r.status in ("failed", "error")):
            st.caption(f"Elapsed: {r.elapsed_sec:.2f}s")
            if r.message:
                st.code(r.message[:2000], language="text")
