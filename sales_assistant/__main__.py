"""Command line entry point: python -m sales_assistant [web|chat|brief|deal|followups|run-jobs|schedule|doctor]"""

from __future__ import annotations

import argparse
import sys
from .agent import SalesAgent, build_connectors, describe_error
from .config import ROOT, get_settings
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
    except Exception as exc:
        message = describe_error(exc)
        if message is None:
            raise
        print(message, file=sys.stderr)
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
            continue
        print(f"\n{answer}\n")


def _job_context(args):
    from .jobs import Context

    ctx = Context.create(effort=args.effort, web_search=not args.no_web)
    conn = ctx.connectors
    print(f"Connected: {', '.join(c.name for c in (conn.crm, conn.meetings, conn.workspace))}", file=sys.stderr)
    return ctx


def cmd_brief(args) -> int:
    from .jobs import run_brief

    ctx = _job_context(args)
    try:
        update = run_brief(ctx, email=args.email or None, create_drafts=True if args.create_drafts else None)
    except Exception as exc:
        message = describe_error(exc)
        if message is None:
            raise
        print(message, file=sys.stderr)
        return 1
    print(update["body"])
    print(f"\nSaved to {ROOT / 'briefs'} and to Updates in the web interface.", file=sys.stderr)
    return 0


def cmd_run_jobs(args) -> int:
    """What the schedule runs: follow up new calls, and write the brief if it is due."""
    from .jobs import run_due

    run_due(_job_context(args), force_brief=args.brief_now)
    return 0


def cmd_followups(args) -> int:
    """Email threads waiting on a reply. No model calls."""
    from datetime import date as _date

    from .followups import find_followups
    from .state import Store

    settings = get_settings()
    items = find_followups(build_connectors(settings), _date.today(), settings.reply_owed_days, settings.no_reply_days, Store().drafted_threads())
    if not items:
        print("Nothing waiting.")
    for item in items:
        label = "You owe a reply" if item["type"] == "reply_owed" else "No reply yet"
        drafted = "  [draft saved]" if item["draft_already_saved"] else ""
        print(f"{label}, {item['days_waiting']}d: {item['subject']}  ({item['deal_name']}){drafted}")
    return 0


def cmd_schedule(args) -> int:
    from . import schedule

    return {"install": schedule.install, "remove": schedule.remove, "status": schedule.status}[args.action]()


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


def cmd_web(args) -> int:
    import threading
    import webbrowser

    from .web import create_app

    app = create_app(effort=args.effort, web_search=not args.no_web)
    url = f"http://127.0.0.1:{args.port}"
    print(f"Sales assistant running at {url}  (Ctrl-C to stop)", file=sys.stderr)
    if not args.no_browser:
        threading.Timer(0.8, webbrowser.open, args=(url,)).start()
    app.run(host="127.0.0.1", port=args.port, threaded=True, use_reloader=False)
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

    web = sub.add_parser("web", parents=[common], help="web interface in your browser")
    web.add_argument("--port", type=int, default=8765)
    web.add_argument("--no-browser", action="store_true", help="do not open a browser window")
    web.set_defaults(func=cmd_web)

    chat = sub.add_parser("chat", parents=[common], help="terminal chat (default)")
    chat.add_argument("prompt", nargs="*", help="optional first message")
    chat.add_argument("--yes", action="store_true", help="perform write actions without asking")
    chat.set_defaults(func=cmd_chat)

    brief = sub.add_parser("brief", parents=[common], help="write the daily pipeline brief now")
    brief.add_argument("--create-drafts", action="store_true", help="save follow-ups as Gmail drafts (default: AUTO_DRAFTS)")
    brief.add_argument("--email", action="store_true", help="also email the brief to yourself")
    brief.set_defaults(func=cmd_brief)

    jobs = sub.add_parser("run-jobs", parents=[common], help="do whatever is due (what the schedule runs)")
    jobs.add_argument("--brief-now", action="store_true", help="write the brief even if it is not due")
    jobs.set_defaults(func=cmd_run_jobs)

    followups = sub.add_parser("followups", help="list email threads waiting on a reply")
    followups.set_defaults(func=cmd_followups)

    sched = sub.add_parser("schedule", help="install or remove the background schedule")
    sched.add_argument("action", choices=["install", "remove", "status"])
    sched.set_defaults(func=cmd_schedule)

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
