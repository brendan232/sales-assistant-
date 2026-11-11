"""The agent: Claude + the tool runner, with a conversation history we own."""

from __future__ import annotations

import sys
from typing import Callable

import anthropic

from .config import Settings
from .connectors.mock import MockCRM, MockMeetings, MockWorkspace
from .prompts import system_blocks, with_date
from .tools import Approver, Connectors, build_tools

MODEL = "claude-opus-5-5"
MAX_TOKENS = 16000
MAX_ITERATIONS = 60


def build_connectors(settings: Settings) -> Connectors:
    """Live connector where credentials exist, sample data otherwise."""
    if settings.hubspot_live:
        from .connectors.hubspot import HubSpotCRM

        crm = HubSpotCRM(settings.hubspot_token, settings.hubspot_owner_id)
    else:
        crm = MockCRM()
    if settings.fathom_live:
        from .connectors.fathom import FathomMeetings

        meetings = FathomMeetings(settings.fathom_api_key)
    else:
        meetings = MockMeetings()
    if settings.google_live:
        from .connectors.google_workspace import GoogleWorkspace

        workspace = GoogleWorkspace(settings.google_client_secrets, settings.google_token_file)
    else:
        workspace = MockWorkspace()
    return Connectors(crm=crm, meetings=meetings, workspace=workspace)


class SalesAgent:
    def __init__(
        self,
        settings: Settings,
        connectors: Connectors,
        approver: Approver,
        *,
        client: anthropic.Anthropic | None = None,
        effort: str = "high",
        web_search: bool = True,
        on_progress: Callable[[str], None] | None = None,
        store=None,
    ) -> None:
        self.client = client or anthropic.Anthropic()
        self.system = system_blocks(settings)
        self.tools = build_tools(connectors, approver, web_search=web_search, store=store, settings=settings)
        self.effort = effort
        self.messages: list = []
        self._progress = on_progress or (lambda line: print(line, file=sys.stderr))

    def ask(self, user_text: str) -> str:
        """Run one rep turn to completion (as many tool calls as it takes) and return the answer text."""
        start = len(self.messages)
        try:
            return self._ask(user_text)
        except BaseException:
            del self.messages[start:]  # drop the unanswered turn so the history stays valid for a retry
            raise

    def _ask(self, user_text: str) -> str:
        self.messages.append({"role": "user", "content": user_text if self.messages else with_date(user_text)})

        runner = self.client.beta.messages.tool_runner(
            model=MODEL,
            max_tokens=MAX_TOKENS,
            system=self.system,
            tools=self.tools,
            messages=self.messages,
            output_config={"effort": self.effort},
            cache_control={"type": "ephemeral"},  # caches the growing conversation; system has its own breakpoint
            # If a safety classifier declines a request, the API retries it on a fallback model in the same call.
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
            max_iterations=MAX_ITERATIONS,
            thread=False,  # send full history each turn; we keep our own copy in self.messages
        )

        answer: list[str] = []
        last = None
        for message in runner:
            last = message
            # Keep the whole assistant turn (thinking and tool_use blocks included) so later turns replay it unchanged.
            self.messages.append(message.to_param())
            for block in message.content:
                if block.type == "text":
                    answer.append(block.text)
                elif block.type in ("tool_use", "server_tool_use"):
                    self._progress(f"  · {block.name} {_brief_args(block.input)}")
            if message.stop_reason == "tool_use":
                results = runner.generate_tool_call_response()  # cached: the runner reuses these, tools run once
                if results is not None:
                    self.messages.append(results)

        if last is None:
            return ""
        if last.stop_reason == "refusal":
            return "The model declined this request. Try rephrasing, or check the deal data it was reading."
        text = "\n".join(t for t in answer if t.strip()).strip()
        if last.stop_reason == "max_tokens":
            text += "\n\n[Answer was cut off at the output limit. Ask me to continue.]"
        elif last.stop_reason == "tool_use":
            text += f"\n\n[Stopped after {MAX_ITERATIONS} steps without finishing. Ask me to continue.]"
        return text


def describe_error(exc: BaseException) -> str | None:
    """A plain-language message for the failures a rep can do something about, else None."""
    if isinstance(exc, anthropic.AuthenticationError):
        return "Claude credentials are missing or invalid. Set ANTHROPIC_API_KEY in .env or run `ant auth login`."
    if isinstance(exc, anthropic.RateLimitError):
        return "Rate limited by the Claude API. Wait a minute and try again."
    if isinstance(exc, anthropic.APIStatusError):
        return f"Claude API error {exc.status_code}: {exc.message}"
    if isinstance(exc, anthropic.APIConnectionError):
        return "Could not reach the Claude API. Check your network connection."
    if isinstance(exc, TypeError) and "authentication method" in str(exc):
        # The SDK raises this before sending anything when it finds no credentials at all.
        return "No Claude credentials found. Set ANTHROPIC_API_KEY in .env or run `ant auth login`."
    return None


def _brief_args(args: object) -> str:
    if not isinstance(args, dict) or not args:
        return ""
    parts = []
    for key, value in args.items():
        value = str(value).replace("\n", " ")
        parts.append(f"{key}={value[:40] + '…' if len(value) > 40 else value}")
    return "(" + ", ".join(parts)[:120] + ")"
