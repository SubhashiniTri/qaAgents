"""Configuration loaded from environment variables."""
from __future__ import annotations

import os
from dataclasses import dataclass, field


class ConfigError(RuntimeError):
    pass


def _strip(val: str) -> str:
    """Strip inline comments and whitespace (e.g. 'value  # comment' -> 'value')."""
    return val.split("#")[0].strip()


def _req(name: str) -> str:
    val = _strip(os.environ.get(name, ""))
    if not val:
        raise ConfigError(f"Missing required environment variable: {name}")
    return val


def _opt(name: str, default: str = "") -> str:
    return _strip(os.environ.get(name, default))


def _int(name: str, default: int | None = None) -> int:
    raw = _strip(os.environ.get(name, ""))
    if not raw:
        if default is None:
            raise ConfigError(f"Missing required environment variable: {name}")
        return default
    return int(raw)


@dataclass
class GitHubConfig:
    token: str
    owner: str
    repo: str
    ref: str = "main"
    # Comma-separated globs/paths searched for requirement text.
    paths: list[str] = field(default_factory=lambda: ["README.md", "docs/"])
    # If set, also pull open issues with this label as requirements.
    issue_label: str = ""
    api_base: str = "https://api.github.com"


@dataclass
class TestRailConfig:
    base_url: str          # e.g. https://yourorg.testrail.io
    user: str              # TestRail account email
    api_key: str           # TestRail API key
    project_id: int
    suite_id: int | None   # required only for multi-suite projects
    section_id: int
    # Optional: TestRail custom template / type / priority ids
    template_id: int | None = None
    type_id: int | None = None
    priority_id: int | None = None


@dataclass
class SlackConfig:
    webhook_url: str = ""   # simplest option
    bot_token: str = ""     # alternative: xoxb-... with chat:write
    channel: str = ""       # required when using bot_token


@dataclass
class JiraConfig:
    base_url: str = ""       # e.g. https://yourorg.atlassian.net
    user: str = ""           # Jira account email
    api_token: str = ""      # Jira API token (from id.atlassian.com/manage-profile/security)
    api_version: str = "3"   # "3" for Cloud, "2" for Server/Data Center


@dataclass
class ModelConfig:
    provider: str          # "anthropic" | "openai"
    api_key: str
    model: str
    max_tokens: int = 16000
    base_url: str = ""     # optional override, e.g. Gemini OpenAI-compat endpoint


@dataclass
class Config:
    github: GitHubConfig
    testrail: TestRailConfig
    slack: SlackConfig
    model_cfg: ModelConfig
    jira: JiraConfig = field(default_factory=JiraConfig)
    num_cases: int = 5
    workdir: str = "./qa_run"

    @classmethod
    def from_env(cls) -> "Config":
        paths = [p.strip() for p in _opt("GITHUB_REQ_PATHS", "README.md,docs/").split(",") if p.strip()]
        suite_raw = _opt("TESTRAIL_SUITE_ID")
        return cls(
            github=GitHubConfig(
                token=_req("GITHUB_TOKEN"),
                owner=_req("GITHUB_OWNER"),
                repo=_req("GITHUB_REPO"),
                ref=_opt("GITHUB_REF", "main"),
                paths=paths,
                issue_label=_opt("GITHUB_ISSUE_LABEL"),
                api_base=_opt("GITHUB_API_BASE", "https://api.github.com"),
            ),
            testrail=TestRailConfig(
                base_url=_req("TESTRAIL_URL").rstrip("/"),
                user=_req("TESTRAIL_USER"),
                api_key=_req("TESTRAIL_API_KEY"),
                project_id=_int("TESTRAIL_PROJECT_ID"),
                suite_id=int(suite_raw) if suite_raw else None,
                section_id=_int("TESTRAIL_SECTION_ID"),
                template_id=_int("TESTRAIL_TEMPLATE_ID", 0) or None,
                type_id=_int("TESTRAIL_TYPE_ID", 0) or None,
                priority_id=_int("TESTRAIL_PRIORITY_ID", 0) or None,
            ),
            slack=SlackConfig(
                webhook_url=_opt("SLACK_WEBHOOK_URL"),
                bot_token=_opt("SLACK_BOT_TOKEN"),
                channel=_opt("SLACK_CHANNEL"),
            ),
            jira=JiraConfig(
                base_url=_opt("JIRA_BASE_URL"),
                user=_opt("JIRA_USER"),
                api_token=_opt("JIRA_API_TOKEN"),
                api_version=_opt("JIRA_API_VERSION", "3"),
            ),
            model_cfg=_model_config(),
            num_cases=_int("NUM_TEST_CASES", 5),
            workdir=_opt("AGENT_WORKDIR", "./qa_run"),
        )


# key_var is None for providers that don't require an API key (e.g. Ollama).
PROVIDER_ENV = {
    "anthropic": ("ANTHROPIC_API_KEY", "ANTHROPIC_MODEL", "claude-sonnet-5"),
    "openai":    ("OPENAI_API_KEY",    "OPENAI_MODEL",    "gpt-4o"),
    "ollama":    (None,                "OLLAMA_MODEL",    "qwen2.5"),
}


def _model_config() -> ModelConfig:
    provider = _opt("MODEL_PROVIDER", "anthropic").lower()
    if provider not in PROVIDER_ENV:
        raise ConfigError(
            f"MODEL_PROVIDER must be one of {', '.join(sorted(PROVIDER_ENV))}, got {provider!r}"
        )
    key_var, model_var, default_model = PROVIDER_ENV[provider]
    if key_var is not None:
        try:
            api_key = _req(key_var)
        except ConfigError:
            raise ConfigError(
                f"MODEL_PROVIDER={provider} requires {key_var} to be set. "
                f"(Note: a ChatGPT or Claude app subscription does not include API access — "
                f"the key must come from the provider's developer console.)"
            ) from None
    else:
        api_key = ""  # Ollama runs locally and needs no key.
    base_url = _opt("OLLAMA_BASE_URL") if provider == "ollama" else _opt("OPENAI_BASE_URL")
    return ModelConfig(
        provider=provider,
        api_key=api_key,
        model=_opt(model_var, default_model),
        max_tokens=_int("MODEL_MAX_TOKENS", 16000),
        base_url=base_url,
    )
