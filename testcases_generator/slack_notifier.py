"""Posts the run summary to Slack via incoming webhook or chat.postMessage."""
from __future__ import annotations

import logging

import requests

from config import SlackConfig
from generator import TestCase
from runner import CaseResult

log = logging.getLogger(__name__)


def build_creation_blocks(*, repo_slug: str, cases: list[TestCase],
                          run_url: str, run_name: str) -> tuple[list[dict], str]:
    """Sent immediately after TestRail cases are created, before test execution."""
    headline = f":white_check_mark: {len(cases)} test case(s) created in TestRail"
    lines = []
    for c in cases:
        cid = f"C{c.testrail_case_id}" if c.testrail_case_id else "—"
        lines.append(f"• [{cid}] *{c.title}* — _{c.priority}_")
    blocks = [
        {"type": "header", "text": {"type": "plain_text", "text": headline[:150]}},
        {"type": "section", "fields": [
            {"type": "mrkdwn", "text": f"*Repo*\n{repo_slug}"},
            {"type": "mrkdwn", "text": f"*Run*\n{run_name}"},
        ]},
        {"type": "divider"},
        {"type": "section", "text": {"type": "mrkdwn", "text": "\n".join(lines)[:2900]}},
    ]
    if run_url:
        blocks.append({"type": "actions", "elements": [
            {"type": "button", "text": {"type": "plain_text", "text": "Open in TestRail"},
             "url": run_url}
        ]})
    return blocks, headline


def build_blocks(*, repo_slug: str, cases: list[TestCase], results: list[CaseResult],
                 summary: dict, run_url: str, run_name: str) -> tuple[list[dict], str]:
    by_key = {c.key: c for c in cases}
    header_icon = ":large_green_circle:" if summary["ok"] else ":red_circle:"
    headline = (f"{header_icon} Testcase Agent run — {summary['passed']}/{summary['total']} passed")

    lines = []
    for r in results:
        c = by_key.get(r.key)
        title = c.title if c else r.key
        cid = f" (C{c.testrail_case_id})" if c and c.testrail_case_id else ""
        detail = ""
        if r.status in ("failed", "error", "untested"):
            first = (r.message or "").splitlines()[0][:140]
            detail = f"\n        ↳ _{first}_" if first else ""
        lines.append(f"{r.emoji} *{title}*{cid}{detail}")
    body = "\n".join(lines)

    counts = (f"Passed: *{summary['passed']}*  |  Failed: *{summary['failed']}*  |  "
              f"Errors: *{summary['error']}*  |  Skipped: *{summary['skipped']}*  |  "
              f"Untested: *{summary['untested']}*")

    blocks = [
        {"type": "header", "text": {"type": "plain_text", "text": headline[:150]}},
        {"type": "section", "fields": [
            {"type": "mrkdwn", "text": f"*Repo*\n{repo_slug}"},
            {"type": "mrkdwn", "text": f"*Run*\n{run_name}"},
        ]},
        {"type": "section", "text": {"type": "mrkdwn", "text": counts}},
        {"type": "divider"},
        {"type": "section", "text": {"type": "mrkdwn", "text": body[:2900]}},
    ]
    if run_url:
        blocks.append({"type": "actions", "elements": [
            {"type": "button", "text": {"type": "plain_text", "text": "Open in TestRail"},
             "url": run_url}
        ]})

    fail_count = summary["failed"] + summary["error"]
    if summary["ok"]:
        verdict = ":rocket: Feature tested successfully — ready for release candidate."
    else:
        verdict = f":warning: {fail_count} test(s) failing — not ready for release."
    blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": f"*Verdict:* {verdict}"}})

    return blocks, headline


def send(cfg: SlackConfig, blocks: list[dict], fallback: str) -> None:
    if cfg.webhook_url:
        r = requests.post(cfg.webhook_url, json={"text": fallback, "blocks": blocks}, timeout=30)
        if r.status_code >= 400:
            raise RuntimeError(f"Slack webhook failed {r.status_code}: {r.text[:300]}")
        log.info("Slack webhook message sent")
        return

    if cfg.bot_token:
        if not cfg.channel:
            raise RuntimeError("SLACK_CHANNEL is required when using SLACK_BOT_TOKEN")
        r = requests.post(
            "https://slack.com/api/chat.postMessage",
            headers={"Authorization": f"Bearer {cfg.bot_token}"},
            json={"channel": cfg.channel, "text": fallback, "blocks": blocks},
            timeout=30,
        )
        data = r.json()
        if not data.get("ok"):
            raise RuntimeError(f"Slack API error: {data.get('error')}")
        log.info("Slack message posted to %s", cfg.channel)
        return

    raise RuntimeError("No Slack credentials configured (SLACK_WEBHOOK_URL or SLACK_BOT_TOKEN)")
