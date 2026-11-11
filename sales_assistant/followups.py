"""Unanswered-email watch.

Deterministic: for each open deal, look at recent threads with that account
and check who spoke last and how long ago. The model is only involved later,
when it writes the reply or the nudge.
"""

from __future__ import annotations

from datetime import date

from .signals import parse_day

LOOKBACK_DAYS = 45
MAX_DEALS = 40


def find_followups(conn, today: date, reply_owed_days: int = 2, no_reply_days: int = 5, drafted: dict | None = None) -> list[dict]:
    """Threads where the rep owes a reply, or where the rep's last email has gone unanswered.

    `drafted` maps thread ids to when the assistant last saved a draft there, so callers can avoid drafting twice.
    """
    drafted = drafted or {}
    me = (conn.workspace.my_address() or "").lower()
    seen: set[str] = set()
    items: list[dict] = []
    for deal in conn.crm.list_open_deals()[:MAX_DEALS]:
        domain = ((deal.get("company") or {}).get("domain") or "").lower()
        if not domain:
            emails = [c["email"] for c in deal.get("contacts", []) if c.get("email")]
            domain = emails[0].split("@")[-1].lower() if emails else ""
        if not domain:
            continue
        query = f"(from:{domain} OR to:{domain}) newer_than:{LOOKBACK_DAYS}d"
        for thread in conn.workspace.search_threads(query, 10):
            if thread["thread_id"] in seen or not thread["messages"]:
                continue
            seen.add(thread["thread_id"])
            last = thread["messages"][-1]
            sent = parse_day(last.get("date"))
            if sent is None:
                continue
            age = (today - sent).days
            from_me = bool(me) and me in (last.get("from") or "").lower()
            if from_me and age >= no_reply_days:
                kind, who = "no_reply", last.get("to", "")
            elif not from_me and age >= reply_owed_days:
                kind, who = "reply_owed", last.get("from", "")
            else:
                continue
            draft_date = parse_day(drafted.get(thread["thread_id"]))
            items.append(
                {
                    "type": kind,
                    "days_waiting": age,
                    "deal_id": deal["id"],
                    "deal_name": deal["name"],
                    "thread_id": thread["thread_id"],
                    "subject": thread["subject"],
                    "counterpart": who,
                    "last_message_date": sent.isoformat(),
                    "last_message_excerpt": " ".join((last.get("body") or "").split())[:400],
                    # A draft saved after the last message means this one is already handled and waiting in Drafts.
                    "draft_already_saved": bool(draft_date and draft_date >= sent),
                }
            )
    items.sort(key=lambda i: (i["type"] != "reply_owed", -i["days_waiting"]))
    return items
