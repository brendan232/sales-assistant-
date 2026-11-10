"""Command line entry point: python -m sales_assistant [chat|brief|deal|doctor]"""

from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path

import anthropic

from .agent import SalesAgent, build_connectors
from .config import ROOT, get_settings
from .prompts import BRIEF
from .tools import Approver


def _agent(args, approver: Approver) -> SalesAgent:
    settings = get_settings()
    connectors = build_connectors(settings)
    names = ", ".join(c.name for c in (connectors.crm, connectors.meetings, connectors.workspace))
    print(f"Connected: {names}", file=sys.stderr)
    return SalesAgent(settings, connectors, approver, effort=args.effort, web_search=not args.no_web)


def _run(agent: SalesAgent, text: str) -> str | None:
    try:
        return agent.ask(text)
    except anthropic.AuthenticationError:
        print("Claude credentials missing or invalid. Set ANTHROPIC_API_KEY or run `ant auth login`.", file=sys.stderr)
    except anthropic.RateLimitError:
        print("Rate limited by the Claude API. Wait a minute and try again.", file=sys.stderr)
    except anthropic.APIStatusError as exc:
        print(f"Claude API error {exc.status_code}: {exc.message}", file=sys.stderr)
    except anthropic.APIConnectionError:
        print("Could not reach the Claude API. Check your network connection.", file=sys.stderr)
    except TypeError as exc:
        if "authentication method" not in str(exc):
            raise
        # The SDK raises this before sending anything when it finds no credentials at all.
        print("No Claude credentials found. Set ANTHROPIC_API_KEY in .env or run `ant auth login`.", file=sys.stderr)
    return None


def cmd_chat(args) -> int:
    agent = _agent(args, Approver("allow" if args.yes else "ask"))
    print("Ask about your pipeline, a deal, or a contact. Try: what should I work on today?  (Ctrl-D to quit)\n")
    first = " ".join(args.prompt) if args.prompt else None
    while True:
        try:
            text = first or input("you> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return 0
        first = None
        if not text:
            continue
        if text in ("exit", "quit"):
            return 0
        answer = _run(agent, text)
        if answer is None:
            # Drop the unanswered turn so the history stays valid for the next attempt.
            del agent.messages[_last_user_index(agent.messages) :]
            continue
        print(f"\n{answer}\n")


def _last_user_index(messages: list) -> int:
    for i in range(len(messages) - 1, -1, -1):
        m = messages[i]
        if m["role"] == "user" and isinstance(m["content"], str):
            return i
    return 0


def cmd_brief(args) -> int:
    agent = _agent(args, Approver("deny", allow_drafts=args.create_drafts))
    prompt = BRIEF
    if args.create_drafts:
        prompt += "\nSave each follow-up email as a draft in my mailbox as well as showing it in the brief.\n"
    answer = _run(agent, prompt)
    if answer is None:
        return 1
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{date.today().isoformat()}.md"
    path.write_text(f"# Pipeline brief, {date.today().isoformat()}\n\n{answer}\n")
    print(answer)
    print(f"\nSaved to {path}", file=sys.stderr)
    return 0


def cmd_deal(args) -> int:
    agent = _agent(args, Approver("deny"))
    answer = _run(
        agent,
        f"Give me a full account plan for the deal matching: {args.name}\n\n"
        "Cover where it stands, the stakeholder map (who we know, who is missing and how to reach them), the "
        "risks, and a step-by-step plan to close it, with drafts for any follow-up due now.",
    )
    if answer is None:
        return 1
    print(answer)
    return 0


def cmd_doctor(args) -> int:
    """Check each connector with one cheap read. No model calls."""
    settings = get_settings()
    ok = True
    try:
        conn = build_connectors(settings)
    except Exception as exc:
        print(f"FAIL  building connectors: {type(exc).__name__}: {exc}")
        return 1
    checks = [
        (conn.crm.name, lambda: f"{len(conn.crm.list_open_deals())} open deals"),
        (conn.meetings.name, lambda: f"{len(conn.meetings.list_meetings('', 30))} calls in the last 30 days"),
        (conn.workspace.name + " mail", lambda: f"{len(conn.workspace.search_threads('newer_than:7d', 3))} recent threads"),
        (conn.workspace.name + " calendar", lambda: f"{len(conn.workspace.upcoming_events(14))} upcoming events"),
    ]
    for name, check in checks:
        try:
            print(f"ok    {name}: {check()}")
        except Exception as exc:
            ok = False
            print(f"FAIL  {name}: {type(exc).__name__}: {exc}")
    return 0 if ok else 1


def main(argv: list[str] | None = None) -> int:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--effort", default="high", choices=["low", "medium", "high", "xhigh", "max"])
    common.add_argument("--no-web", action="store_true", help="disable web search")

    parser = argparse.ArgumentParser(prog="sales_assistant", description="A proactive sales assistant.")
    sub = parser.add_subparsers(dest="command")

    chat = sub.add_parser("chat", parents=[common], help="interactive chat (default)")
    chat.add_argument("prompt", nargs="*", help="optional first message")
    chat.add_argument("--yes", action="store_true", help="perform write actions without asking")
    chat.set_defaults(func=cmd_chat)

    brief = sub.add_parser("brief", parents=[common], help="unattended daily pipeline brief")
    brief.add_argument("--create-drafts", action="store_true", help="also save follow-ups as Gmail drafts")
    brief.add_argument("--out", default=str(ROOT / "briefs"), help="directory for the saved brief")
    brief.set_defaults(func=cmd_brief)

    deal = sub.add_parser("deal", parents=[common], help="account plan for one deal")
    deal.add_argument("name", help="deal or company name")
    deal.set_defaults(func=cmd_deal)

    doctor = sub.add_parser("doctor", help="check connector credentials")
    doctor.set_defaults(func=cmd_doctor)

    args = parser.parse_args(argv)
    if not args.command:
        args = parser.parse_args(["chat", *(argv or [])])
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
