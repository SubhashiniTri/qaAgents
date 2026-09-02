"""Model providers.

Both providers accept the same JSON Schema and return the same dict:

    {"cases": [...], "pytest_module": "...", "notes": "..."}

Everything downstream of this module is provider-agnostic — it only ever sees
`TestCase` objects, never a raw API response.
"""
from __future__ import annotations

import json
import logging
import re
import time
from abc import ABC, abstractmethod

log = logging.getLogger(__name__)

TOOL_NAME = "emit_test_cases"
TOOL_DESCRIPTION = "Emit the generated test cases and the pytest module that executes them."

DEFAULT_MODELS = {
    "anthropic": "claude-sonnet-5",
    # Override with OPENAI_MODEL to whatever your account has access to.
    "openai": "gpt-4o",
    "ollama": "qwen2.5",
}


class ProviderError(RuntimeError):
    pass


class ModelProvider(ABC):
    """Sends the prompt, forces a structured tool call, returns the parsed payload."""

    name: str

    def __init__(self, api_key: str, model: str, max_tokens: int = 16000, base_url: str = ""):
        if not api_key:
            raise ProviderError(f"{self.name}: missing API key")
        self.api_key = api_key
        self.model = model
        self.max_tokens = max_tokens
        self.base_url = base_url

    @abstractmethod
    def generate(self, *, system: str, prompt: str, schema: dict) -> dict:
        """Return the tool-call arguments as a dict."""

    def _log_call(self) -> None:
        log.info("Generating test cases via %s (%s)", self.name, self.model)


class AnthropicProvider(ModelProvider):
    name = "anthropic"

    def generate(self, *, system: str, prompt: str, schema: dict) -> dict:
        from anthropic import Anthropic

        self._log_call()
        client = Anthropic(api_key=self.api_key)
        resp = client.messages.create(
            model=self.model,
            max_tokens=self.max_tokens,
            system=system,
            tools=[{
                "name": TOOL_NAME,
                "description": TOOL_DESCRIPTION,
                "input_schema": schema,
            }],
            tool_choice={"type": "tool", "name": TOOL_NAME},
            messages=[{"role": "user", "content": prompt}],
        )
        for block in resp.content:
            if getattr(block, "type", None) == "tool_use" and block.name == TOOL_NAME:
                return dict(block.input)
        raise ProviderError(f"anthropic: model did not return a {TOOL_NAME} tool call")


class OpenAIProvider(ModelProvider):
    name = "openai"

    def generate(self, *, system: str, prompt: str, schema: dict) -> dict:
        from openai import OpenAI, RateLimitError

        self._log_call()
        client = OpenAI(api_key=self.api_key, **(({"base_url": self.base_url}) if self.base_url else {}))
        last_err: RateLimitError | None = None
        resp = None
        for attempt in range(4):
            try:
                resp = client.chat.completions.create(
                    model=self.model,
                    max_tokens=self.max_tokens,   # max_completion_tokens not supported by all compat layers
                    messages=[
                        {"role": "system", "content": system},
                        {"role": "user", "content": prompt},
                    ],
                    tools=[{
                        "type": "function",
                        "function": {
                            "name": TOOL_NAME,
                            "description": TOOL_DESCRIPTION,
                            "parameters": schema,
                        },
                    }],
                    tool_choice={"type": "function", "function": {"name": TOOL_NAME}},
                )
                last_err = None
                break
            except RateLimitError as e:
                last_err = e
                delay = 60
                m = re.search(r"retry in (\d+(?:\.\d+)?)s", str(e), re.IGNORECASE)
                if m:
                    delay = int(float(m.group(1))) + 2
                log.warning("Rate limited (429); retrying in %ds (attempt %d/4)…", delay, attempt + 1)
                time.sleep(delay)
        if last_err:
            raise last_err
        calls = resp.choices[0].message.tool_calls or []
        for call in calls:
            if call.function.name == TOOL_NAME:
                try:
                    return json.loads(call.function.arguments)
                except json.JSONDecodeError as e:
                    raise ProviderError(f"openai: tool arguments were not valid JSON: {e}") from e
        raise ProviderError(f"openai: model did not return a {TOOL_NAME} tool call")


class OllamaProvider(ModelProvider):
    """Ollama local models via the OpenAI-compatible /v1 endpoint."""

    name = "ollama"
    _DEFAULT_BASE_URL = "http://localhost:11434/v1"

    def __init__(self, api_key: str, model: str, max_tokens: int = 16000, base_url: str = ""):
        self.api_key = "ollama"
        self.model = model
        # Ollama needs more output tokens — local models are verbose with JSON.
        self.max_tokens = max(max_tokens, 32000)
        self.base_url = base_url or self._DEFAULT_BASE_URL

    def generate(self, *, system: str, prompt: str, schema: dict) -> dict:
        from openai import OpenAI

        self._log_call()
        client = OpenAI(api_key=self.api_key, base_url=self.base_url)

        n = re.search(r"Generate exactly (\d+) test cases", prompt)
        count_hint = f" Generate EXACTLY {n.group(1)} items in the 'cases' array." if n else ""

        # --- Pass 1: generate cases only (small JSON, won't truncate) ---
        cases_system = (
            system + "\n\n"
            "IMPORTANT: reply with ONLY a raw JSON object — no markdown, no prose."
            + count_hint + "\n"
            "Return ONLY the cases — do NOT include pytest_module yet.\n"
            "{\n"
            '  "cases": [\n'
            '    { "key": "snake_case_max40", "title": "...", "requirement_source": "...",\n'
            '      "preconditions": "...",\n'
            '      "priority": "Low"|"Medium"|"High"|"Critical",\n'
            '      "steps": [{ "step": "...", "expected": "..." }] }\n'
            "  ],\n"
            '  "notes": "<optional>"\n'
            "}"
        )
        log.info("Ollama pass 1/2: generating test cases…")
        resp1 = client.chat.completions.create(
            model=self.model,
            max_tokens=self.max_tokens,
            messages=[
                {"role": "system", "content": cases_system},
                {"role": "user", "content": prompt},
            ],
        )
        cases_json = self._parse_json((resp1.choices[0].message.content or "").strip())

        cases_list = cases_json.get("cases", [])
        if not cases_list:
            raise ProviderError("ollama: model returned zero cases in pass 1")

        # --- Pass 2: generate pytest module given the cases ---
        keys = [c.get("key", "") for c in cases_list]
        fn_list = ", ".join(f"test_{k}" for k in keys if k)
        code_system = (
            "You are a senior QA engineer. Write a complete, self-contained, runnable "
            "pytest module (Python code only). The module must:\n"
            "- Define stubs/fixtures as needed (no external services, no network).\n"
            "- Contain EXACTLY these test functions: " + fn_list + "\n"
            "- Each function must have meaningful assertions, never `assert True`.\n\n"
            "Reply with ONLY the raw Python code — no markdown fences, no explanation."
        )
        cases_summary = "\n".join(
            f"- test_{c.get('key', 'unknown')}: {c.get('title', '')} "
            f"(steps: {'; '.join(s.get('step', '') for s in c.get('steps', []))})"
            for c in cases_list
        )
        log.info("Ollama pass 2/2: generating pytest module for %d cases…", len(cases_list))
        resp2 = client.chat.completions.create(
            model=self.model,
            max_tokens=self.max_tokens,
            messages=[
                {"role": "system", "content": code_system},
                {"role": "user", "content": f"Generate the pytest module for these test cases:\n{cases_summary}"},
            ],
        )
        code = (resp2.choices[0].message.content or "").strip()
        # Strip markdown fences if present
        m = re.search(r"```(?:python)?\s*([\s\S]+?)```", code)
        if m:
            code = m.group(1).strip()

        cases_json["pytest_module"] = code
        return cases_json

    @staticmethod
    def _parse_json(text: str) -> dict:
        """Try progressively looser strategies to extract a JSON object."""
        # 1. clean parse
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            pass
        # 2. strip markdown fences
        m = re.search(r"```(?:json)?\s*([\s\S]+?)```", text)
        if m:
            try:
                return json.loads(m.group(1).strip())
            except json.JSONDecodeError:
                pass
        # 3. grab the outermost {...} block
        m = re.search(r"\{[\s\S]*\}", text)
        if m:
            try:
                return json.loads(m.group(0))
            except json.JSONDecodeError:
                pass
        # 4. repair truncated JSON — close open brackets/braces
        raw = text.strip()
        m2 = re.search(r"\{", raw)
        if m2:
            raw = raw[m2.start():]
            repaired = OllamaProvider._repair_truncated_json(raw)
            if repaired is not None:
                return repaired
        raise ProviderError(
            f"ollama: could not extract valid JSON from model response.\n"
            f"Tip: try a larger model (e.g. qwen2.5:14b) or increase NUM_TEST_CASES.\n"
            f"Raw output (first 600 chars):\n{text[:600]}"
        )

    @staticmethod
    def _repair_truncated_json(text: str) -> dict | None:
        """Attempt to close brackets/braces on truncated JSON and parse."""
        # Walk the string tracking open delimiters
        stack: list[str] = []
        in_string = False
        escape = False
        for ch in text:
            if escape:
                escape = False
                continue
            if ch == '\\':
                if in_string:
                    escape = True
                continue
            if ch == '"' and not escape:
                in_string = not in_string
                continue
            if in_string:
                continue
            if ch in ('{', '['):
                stack.append(ch)
            elif ch == '}':
                if stack and stack[-1] == '{':
                    stack.pop()
            elif ch == ']':
                if stack and stack[-1] == '[':
                    stack.pop()
        if not stack:
            return None  # not truncated; earlier parse should have worked
        # Close open structures in reverse and try to parse
        if in_string:
            text += '"'
        closers = {'[': ']', '{': '}'}
        suffix = ''.join(closers.get(c, '') for c in reversed(stack))
        try:
            result = json.loads(text + suffix)
            log.warning("Repaired truncated JSON from Ollama (closed %d open brackets)", len(stack))
            return result
        except json.JSONDecodeError:
            return None


PROVIDERS: dict[str, type[ModelProvider]] = {
    "anthropic": AnthropicProvider,
    "openai": OpenAIProvider,
    "ollama": OllamaProvider,
}


def build_provider(name: str, api_key: str, model: str, max_tokens: int = 16000, base_url: str = "") -> ModelProvider:
    key = (name or "").strip().lower()
    if key not in PROVIDERS:
        raise ProviderError(
            f"Unknown MODEL_PROVIDER {name!r}. Supported: {', '.join(sorted(PROVIDERS))}"
        )
    return PROVIDERS[key](api_key=api_key, model=model, max_tokens=max_tokens, base_url=base_url)
