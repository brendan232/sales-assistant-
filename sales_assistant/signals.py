"""Deterministic deal-risk signals.

These are computed in code rather than left to the model so the daily brief is
consistent and cheap: the model spends its effort on *what to do about* a
flagged deal, not on date arithmetic.
"""

from __future__ import annotations

import re
from datetime import date, datetime

STALE_DAYS = 7
VERY_STALE_DAYS = 14

_SENIOR = re.compile(
    r"\b(chief|c[efiotmr]o|cro|ciso|founder|owner|president|vp|vice president|svp|evp|head of|director|gm|general manager)\b",
    re.I,
)


def parse_day(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).date()
    except ValueError:
        try:
            return date.fromisoformat(value[:10])
        except ValueError:
            return None


def is_senior(title: str | None) -> bool:
    return bool(title and _SENIOR.search(title))


def deal_flags(deal: dict, today: date, has_upcoming_meeting: bool | None = None) -> list[dict]:
    """Return a list of {flag, severity, detail} for one normalised deal."""
    flags: list[dict] = []

    def add(flag: str, severity: str, detail: str) -> None:
        flags.append({"flag": flag, "severity": severity, "detail": detail})

    last = parse_day(deal.get("last_activity_at"))
    if last is None:
        add("no_activity_logged", "medium", "No activity recorded on this deal.")
    else:
        idle = (today - last).days
        if idle >= VERY_STALE_DAYS:
            add("stalled", "high", f"No activity for {idle} days.")
        elif idle >= STALE_DAYS:
            add("going_quiet", "medium", f"No activity for {idle} days.")

    contacts = deal.get("contacts") or []
    if len(contacts) == 0:
        add("no_contacts", "high", "No contacts associated with the deal.")
    elif len(contacts) == 1:
        add("single_threaded", "high", f"Only one contact: {contacts[0].get('name') or contacts[0].get('email')}.")
    if contacts and not any(is_senior(c.get("title")) for c in contacts):
        add("no_senior_contact", "medium", "No director-level or above contact on the deal.")

    close = parse_day(deal.get("close_date"))
    if close is None:
        add("no_close_date", "low", "No close date set.")
    elif close < today:
        add("close_date_passed", "high", f"Close date {close.isoformat()} is {(today - close).days} days in the past.")
    elif (close - today).days <= 14:
        add("closing_soon", "medium", f"Close date is in {(close - today).days} days.")

    if not (deal.get("next_step") or "").strip():
        add("no_next_step", "medium", "No next step recorded.")

    if has_upcoming_meeting is False:
        add("no_meeting_booked", "medium", "No upcoming meeting with this account on the calendar.")

    return flags


_WEIGHT = {"high": 3, "medium": 2, "low": 1}


def risk_score(flags: list[dict]) -> int:
    return sum(_WEIGHT[f["severity"]] for f in flags)
