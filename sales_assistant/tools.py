"""The agent's tool surface.

Reads run freely. Anything that changes an outside system goes through the
Approver, so the rep (or the unattended-mode policy) decides, not the model.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date
from typing import Callable

from anthropic import beta_tool
from anthropic.lib.tools import ToolError

from .signals import deal_flags, parse_day, risk_score

WEB_SEARCH = {"type": "web_search_20260209", "name": "web_search", "max_uses": 6}


@dataclass
class Connectors:
    crm: object
    meetings: object
    workspace: object


class Approver:
    """Gate for write actions.

    mode "ask":   prompt the rep in the terminal (interactive chat).
    mode "allow": perform without asking.
    mode "deny":  never perform; the agent lists the action as a proposal instead (unattended runs).
    `allow_drafts` lets Gmail drafts through in "deny" mode - a draft is never sent and is easy to discard.
    """

    def __init__(self, mode: str = "ask", allow_drafts: bool = False, prompt: Callable[[str], str] = input) -> None:
        self.mode = mode
        self.allow_drafts = allow_drafts
        self._prompt = prompt

    def check(self, kind: str, preview: str) -> str | None:
        """Return None if approved, otherwise the message to hand back to the model."""
        if self.mode == "allow" or (kind == "draft" and self.allow_drafts and self.mode == "deny"):
            return None
        if self.mode == "deny":
            return (
                "Not performed: this run is unattended and write actions are disabled. "
                "Include the full content in your answer as a proposed action for the rep."
            )
        print(f"\n--- approval needed: {kind} ---\n{preview}\n")
        answer = self._prompt("Do it? [y/N] ").strip().lower()
        if answer in ("y", "yes"):
            return None
        return "The rep declined this action. Do not retry it; ask what they would like changed."


def _json(value) -> str:
    return json.dumps(value, indent=1, default=str)


def _guard(fn: Callable[[], object]) -> str:
    try:
        return _json(fn())
    except ToolError:
        raise
    except Exception as exc:  # surfaced to the model as is_error so it can adapt or report
        raise ToolError(f"{type(exc).__name__}: {exc}") from exc


def pipeline_snapshot(conn: Connectors) -> dict:
    """Every open deal with risk flags, riskiest first. Pure data, no model call (the web UI uses it too)."""
    today = date.today()
    try:
        events = conn.workspace.upcoming_events(21)
    except Exception:
        events = None
    rows = []
    for deal in conn.crm.list_open_deals():
        domain = (deal.get("company") or {}).get("domain") or ""
        booked = None
        if events is not None and domain:
            booked = any(a.endswith("@" + domain) for e in events for a in e["attendees"])
        flags = deal_flags(deal, today, booked)
        last = parse_day(deal.get("last_activity_at"))
        rows.append(
            {
                "id": deal["id"],
                "name": deal["name"],
                "amount": deal.get("amount"),
                "stage": deal.get("stage"),
                "close_date": deal.get("close_date"),
                "days_since_activity": (today - last).days if last else None,
                "next_step": deal.get("next_step"),
                "company": deal.get("company"),
                "contacts": [{"name": c.get("name"), "title": c.get("title")} for c in deal.get("contacts", [])],
                "risk_score": risk_score(flags),
                "flags": flags,
            }
        )
    rows.sort(key=lambda r: (r["risk_score"], r["amount"] or 0), reverse=True)
    return {"today": today.isoformat(), "open_deals": rows}


def build_tools(conn: Connectors, approver: Approver, web_search: bool = True) -> list:
    @beta_tool
    def pipeline_review() -> str:
        """List every open deal with computed risk flags, sorted so the deals needing attention come first.

        Use this first for any question about the pipeline as a whole, what to work on, or which deals are at risk.
        Flags include: stalled / going_quiet (days without activity), single_threaded, no_senior_contact,
        close_date_passed, closing_soon, no_next_step, no_meeting_booked. Flags are heuristics computed from CRM
        and calendar data, so confirm against emails and call notes before acting on one.
        """

        return _guard(lambda: pipeline_snapshot(conn))

    @beta_tool
    def get_deal(deal_id: str) -> str:
        """Get one deal in full: stage, amount, dates, company, every associated contact with title and email, and recent CRM notes.

        Args:
            deal_id: The deal id from pipeline_review.
        """
        return _guard(lambda: conn.crm.get_deal(deal_id))

    @beta_tool
    def list_meetings(company_domain: str = "", days_back: int = 30) -> str:
        """List recorded sales calls with their AI summary, action items and attendees.

        Args:
            company_domain: Only calls with an attendee at this email domain, e.g. "acme.com". Empty for all calls.
            days_back: How far back to look.
        """
        return _guard(lambda: conn.meetings.list_meetings(company_domain, days_back))

    @beta_tool
    def get_meeting_transcript(meeting_id: str) -> str:
        """Get the full transcript of one recorded call. Use it when the summary is not enough, for example to quote what a buyer said, find names mentioned in passing, or check what was promised.

        Args:
            meeting_id: The meeting id from list_meetings.
        """
        return _guard(lambda: conn.meetings.get_transcript(meeting_id))

    @beta_tool
    def search_email(query: str, max_results: int = 8) -> str:
        """Search the rep's mailbox and return matching threads with their most recent messages.

        Args:
            query: Gmail search syntax, e.g. "from:acme.com newer_than:30d" or a contact's email address.
            max_results: Maximum number of threads.
        """
        return _guard(lambda: conn.workspace.search_threads(query, max_results))

    @beta_tool
    def get_calendar(days_ahead: int = 14) -> str:
        """List the rep's upcoming calendar events with attendee emails.

        Args:
            days_ahead: How many days ahead to look.
        """
        return _guard(lambda: conn.workspace.upcoming_events(days_ahead))

    @beta_tool
    def create_email_draft(to: str, subject: str, body: str, thread_id: str = "") -> str:
        """Save an email as a draft in the rep's mailbox. It is never sent; the rep reviews and sends it themselves.

        Args:
            to: Recipient email address(es), comma separated.
            subject: Subject line. For a reply, reuse the thread's subject.
            body: Plain-text body, ready to send as written.
            thread_id: Thread id from search_email to draft a reply in that thread. Empty for a new email.
        """
        refusal = approver.check("draft", f"To: {to}\nSubject: {subject}\n\n{body}")
        if refusal:
            return refusal
        return _guard(lambda: conn.workspace.create_draft(to, subject, body, thread_id))

    @beta_tool
    def log_crm_note(deal_id: str, body: str) -> str:
        """Add a note to a deal in the CRM. Requires the rep's approval.

        Args:
            deal_id: The deal to attach the note to.
            body: The note text.
        """
        refusal = approver.check("CRM note", f"Deal {deal_id}\n\n{body}")
        if refusal:
            return refusal
        return _guard(lambda: conn.crm.add_note(deal_id, body))

    @beta_tool
    def create_crm_task(deal_id: str, title: str, due_date: str, notes: str = "") -> str:
        """Create a follow-up task on a deal in the CRM. Requires the rep's approval.

        Args:
            deal_id: The deal the task belongs to.
            title: Short imperative title, e.g. "Send security pack to Greg".
            due_date: Due date as YYYY-MM-DD.
            notes: Optional detail.
        """
        refusal = approver.check("CRM task", f"Deal {deal_id}\n{title} (due {due_date})\n{notes}")
        if refusal:
            return refusal
        return _guard(lambda: conn.crm.create_task(deal_id, title, due_date, notes))

    tools: list = [
        pipeline_review, get_deal, list_meetings, get_meeting_transcript,
        search_email, get_calendar, create_email_draft, log_crm_note, create_crm_task,
    ]
    if web_search:
        tools.append(WEB_SEARCH)
    return tools
