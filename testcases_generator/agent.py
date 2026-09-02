#!/usr/bin/env python3
"""
Testcase Agent
==============
GitHub requirements -> generated test cases -> TestRail -> execute -> Slack status.

Usage:
    python agent.py                 # full pipeline
    python agent.py --dry-run       # no TestRail writes, no Slack post
    python agent.py --skip-slack
    python agent.py --from-fixture fixtures/sample_requirements.md   # offline requirements
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(name)s | %(message)s")
log = logging.getLogger("agent")

try:
    from dotenv import load_dotenv
    load_dotenv(dotenv_path=Path(__file__).parent / ".env", override=True)
except ImportError:
    pass

from config import Config, ConfigError                     # noqa: E402
from github_reader import GitHubReader, RequirementDoc     # noqa: E402
from jira_reader import JiraReader                         # noqa: E402
from generator import TestCaseGenerator, dump_cases        # noqa: E402
from testrail_client import (                              # noqa: E402
    TestRailClient, STATUS_PASSED, STATUS_FAILED, STATUS_BLOCKED, STATUS_UNTESTED,
)
import runner                                              # noqa: E402
import slack_notifier                                      # noqa: E402

STATUS_TO_TESTRAIL = {
    "passed": STATUS_PASSED,
    "failed": STATUS_FAILED,
    "error": STATUS_FAILED,
    "skipped": STATUS_BLOCKED,
    "untested": STATUS_UNTESTED,
}


def parse_args():
    p = argparse.ArgumentParser(description="Testcase Agent")
    p.add_argument("--dry-run", action="store_true",
                   help="Generate and execute tests, but skip TestRail writes and Slack post")
    p.add_argument("--skip-slack", action="store_true")
    p.add_argument("--skip-testrail", action="store_true")
    p.add_argument("--from-fixture", metavar="PATH",
                   help="Read requirements from a local file instead of GitHub")
    p.add_argument("--close-run", action="store_true", help="Close the TestRail run when done")
    p.add_argument("--issue", metavar="NUMBER", type=int,
                   help="Generate test cases for a single GitHub issue number")
    p.add_argument("--jira-issue", metavar="KEY",
                   help="Fetch requirement from a Jira issue key (e.g. PROJ-123)")
    p.add_argument("--jira-jql", metavar="JQL",
                   help="Fetch requirements via a Jira JQL query (e.g. project=PROJ AND type=Story)")
    p.add_argument("--review", action="store_true",
                   help="Pause after generation to review and approve cases before uploading")
    return p.parse_args()


def review_and_filter_cases(cases):
    """Print generated cases and return the approved subset. Returns None to abort."""
    sep = "-" * 70
    print(f"\n{sep}")
    print(f"  REVIEW GENERATED TEST CASES  ({len(cases)} total)")
    print(sep)
    for i, c in enumerate(cases, 1):
        print(f"\n[{i}] {c.title}  |  priority={c.priority}  |  source={c.requirement_source}")
        if c.preconditions:
            print(f"    Preconditions: {c.preconditions}")
        for j, s in enumerate(c.steps, 1):
            print(f"    Step {j}: {s.step}")
            print(f"           Expected: {s.expected}")
    print(f"\n{sep}")
    print("Options:")
    print("  a          — approve all and continue")
    print("  1,3,5      — approve only listed numbers (comma-separated)")
    print("  n          — abort (nothing will be uploaded)")
    print(sep)
    while True:
        try:
            choice = input("Your choice: ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print("\nAborted.")
            return None
        if choice == "a":
            return cases
        if choice == "n":
            print("Aborted by user.")
            return None
        try:
            indices = [int(x.strip()) for x in choice.split(",")]
            selected = [cases[i - 1] for i in indices if 1 <= i <= len(cases)]
            if not selected:
                print("No valid indices — try again.")
                continue
            print(f"Approved {len(selected)} of {len(cases)} cases.")
            return selected
        except ValueError:
            print("Invalid input — enter 'a', 'n', or comma-separated numbers.")


def load_requirements(cfg: Config, fixture: str | None,
                      issue: int | None = None,
                      jira_issue: str | None = None,
                      jira_jql: str | None = None):
    """Collect RequirementDocs from any combination of sources."""
    docs: list[RequirementDoc] = []
    slugs: list[str] = []

    if fixture:
        text = Path(fixture).read_text(encoding="utf-8")
        log.info("Loaded requirements from fixture %s (%d chars)", fixture, len(text))
        docs.append(RequirementDoc(source=fixture, title=Path(fixture).name, text=text))
        slugs.append(f"fixture:{Path(fixture).name}")

    if jira_issue:
        jr = JiraReader(cfg.jira)
        log.info("Fetching Jira issue %s", jira_issue)
        docs.append(jr.fetch_issue(jira_issue))
        slugs.append(f"jira:{jira_issue}")

    if jira_jql:
        jr = JiraReader(cfg.jira)
        jira_docs = jr.fetch_by_jql(jira_jql)
        docs.extend(jira_docs)
        slugs.append(f"jira-jql")

    # GitHub sources — only fetched when explicitly requested or no other source given
    if issue is not None:
        reader = GitHubReader(cfg.github)
        log.info("Fetching GitHub issue #%d", issue)
        gh_docs = reader.fetch_issue(issue)
        docs.extend(gh_docs if isinstance(gh_docs, list) else [gh_docs])
        slugs.append(reader.repo_slug())
    elif not fixture and not jira_issue and not jira_jql:
        reader = GitHubReader(cfg.github)
        gh_docs = reader.fetch()
        docs.extend(gh_docs)
        slugs.append(reader.repo_slug())

    repo_slug = " + ".join(slugs) if slugs else "unknown"
    return docs, repo_slug


def main() -> int:
    args = parse_args()
    try:
        cfg = Config.from_env()
    except ConfigError as e:
        log.error("%s  (copy .env.example to .env and fill it in)", e)
        return 2

    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    run_name = f"Testcase Agent run — {stamp} ({cfg.model_cfg.model})"
    workdir = Path(cfg.workdir)
    workdir.mkdir(parents=True, exist_ok=True)

    # 1. requirements
    docs, repo_slug = load_requirements(cfg, args.from_fixture, args.issue,
                                        args.jira_issue, args.jira_jql)

    # 2. generate
    log.info("Model provider: %s (%s)", cfg.model_cfg.provider, cfg.model_cfg.model)
    gen = TestCaseGenerator(cfg.model_cfg, cfg.num_cases)
    cases, code, notes = gen.generate(docs, repo_slug)
    (workdir / "test_cases.json").write_text(dump_cases(cases), encoding="utf-8")
    if notes:
        log.info("Generator notes: %s", notes)

    # 2b. optional interactive review before upload
    if args.review:
        cases = review_and_filter_cases(cases)
        if cases is None:
            return 1
        (workdir / "test_cases.json").write_text(dump_cases(cases), encoding="utf-8")

    # 3. upload to TestRail
    tr = None
    run_id = None
    if not (args.dry_run or args.skip_testrail):
        tr = TestRailClient(cfg.testrail)
        case_ids = tr.upload_cases(cases, run_name)
        run_id = tr.add_run(
            run_name,
            f"Auto-generated from {repo_slug} by testcase-agent.\n\n{notes}".strip(),
            case_ids,
        )
    else:
        log.info("Skipping TestRail upload (dry-run/skip flag)")

    run_url = tr.run_url(run_id) if (tr and run_id) else ""

    # 3b. notify Slack that cases are created and ready for execution
    if tr and run_id and not (args.dry_run or args.skip_slack):
        cr_blocks, cr_fallback = slack_notifier.build_creation_blocks(
            repo_slug=repo_slug, cases=cases, run_url=run_url, run_name=run_name)
        slack_notifier.send(cfg.slack, cr_blocks, cr_fallback)
        log.info("Slack creation notification sent")

    # 4. execute
    test_path = runner.write_test_module(str(workdir), code)
    rc, output, junit = runner.run_pytest(str(workdir), test_path)
    (workdir / "pytest_output.txt").write_text(output, encoding="utf-8")
    results = runner.parse_junit(junit, cases)
    summary = runner.summarize(results)
    log.info("Test summary: %s", {k: v for k, v in summary.items() if k != "ok"})

    # 5. push results
    if tr and run_id:
        payload = [{
            "case_id": c.testrail_case_id,
            "status_id": STATUS_TO_TESTRAIL.get(r.status, STATUS_UNTESTED),
            "comment": f"testcase-agent\nStatus: {r.status}\n\n{r.message}"[:8000],
            "elapsed": f"{max(1, round(r.elapsed_sec))}s" if r.elapsed_sec else None,
        } for c, r in zip(cases, results) if c.testrail_case_id]
        for p in payload:
            if p["elapsed"] is None:
                p.pop("elapsed")
        tr.add_results(run_id, payload)
        if args.close_run:
            tr.close_run(run_id)

    # 6. slack — final results + PM verdict
    blocks, fallback = slack_notifier.build_blocks(
        repo_slug=repo_slug, cases=cases, results=results,
        summary=summary, run_url=run_url, run_name=run_name,
    )
    (workdir / "slack_payload.json").write_text(
        json.dumps({"text": fallback, "blocks": blocks}, indent=2), encoding="utf-8")

    if args.dry_run or args.skip_slack:
        log.info("Skipping Slack post (dry-run/skip flag). Payload written to %s",
                 workdir / "slack_payload.json")
    else:
        slack_notifier.send(cfg.slack, blocks, fallback)

    print("\n" + fallback)
    print(f"Artifacts: {workdir.resolve()}")
    if run_url:
        print(f"TestRail run: {run_url}")

    return 0 if summary["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
