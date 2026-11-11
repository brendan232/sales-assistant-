"""Sample-data connectors so the agent runs end to end with no credentials.

They implement the same methods as the live connectors. Writes are appended to
.mock_outbox.jsonl so you can see what the agent would have created.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from ..config import ROOT

SAMPLE = ROOT / "data" / "mock" / "sample.json"
OUTBOX = ROOT / ".mock_outbox.jsonl"


def _day(offset: int) -> str:
    return (date.today() + timedelta(days=offset)).isoformat()


def _load() -> dict:
    return json.loads(SAMPLE.read_text())


def _record(kind: str, payload: dict) -> dict:
    entry = {"kind": kind, "at": datetime.now(timezone.utc).isoformat(timespec="seconds"), **payload}
    with OUTBOX.open("a") as fh:
        fh.write(json.dumps(entry) + "\n")
    return {"status": "created (sample mode - written to .mock_outbox.jsonl)", "id": f"mock-{kind}"}


class MockCRM:
    name = "HubSpot (sample data)"

    def __init__(self) -> None:
        self._deals = _load()["deals"]

    def _norm(self, d: dict, with_notes: bool) -> dict:
        out = {
            "id": d["id"],
            "name": d["name"],
            "amount": d["amount"],
            "stage": d["stage"],
            "close_date": _day(d["close_days"]),
            "created_at": _day(d["created_days"]),
            "last_activity_at": _day(d["last_activity_days"]),
            "next_step": d["next_step"],
            "company": d["company"],
            "contacts": d["contacts"],
        }
        if with_notes:
            out["notes"] = [{"date": _day(n["days"]), "body": n["body"]} for n in d["notes"]]
        return out

    def list_open_deals(self) -> list[dict]:
        return [self._norm(d, with_notes=False) for d in self._deals]

    def get_deal(self, deal_id: str) -> dict:
        for d in self._deals:
            if d["id"] == deal_id:
                return self._norm(d, with_notes=True)
        raise LookupError(f"No deal with id {deal_id!r}")

    def add_note(self, deal_id: str, body: str) -> dict:
        self.get_deal(deal_id)
        return _record("crm_note", {"deal_id": deal_id, "body": body})

    def create_task(self, deal_id: str, title: str, due_date: str, notes: str) -> dict:
        self.get_deal(deal_id)
        return _record("crm_task", {"deal_id": deal_id, "title": title, "due_date": due_date, "notes": notes})


class MockMeetings:
    name = "Fathom (sample data)"

    def __init__(self) -> None:
        self._meetings = _load()["meetings"]

    def list_meetings(self, domain: str = "", days_back: int = 30) -> list[dict]:
        out = []
        for m in self._meetings:
            if -m["days"] > days_back:
                continue
            if domain and not any(a["email"].endswith("@" + domain) for a in m["attendees"]):
                continue
            out.append(
                {
                    "id": m["id"],
                    "title": m["title"],
                    "date": _day(m["days"]),
                    "attendees": m["attendees"],
                    "summary": m["summary"],
                    "action_items": m["action_items"],
                }
            )
        return out

    def get_transcript(self, meeting_id: str) -> dict:
        for m in self._meetings:
            if m["id"] == meeting_id:
                return {"id": m["id"], "title": m["title"], "date": _day(m["days"]), "transcript": m["transcript"]}
        raise LookupError(f"No meeting with id {meeting_id!r}")


class MockWorkspace:
    name = "Gmail + Calendar (sample data)"

    def __init__(self) -> None:
        data = _load()
        self._threads = data["email_threads"]
        self._events = data["calendar"]

    def search_threads(self, query: str, max_results: int = 10) -> list[dict]:
        terms = []
        for token in query.lower().replace("(", " ").replace(")", " ").split():
            if token == "or":
                continue
            key, sep, value = token.partition(":")
            if not sep:
                terms.append(token)
            elif key in ("from", "to", "cc"):
                terms.append(value)
            # other Gmail operators (newer_than:, label:, ...) are ignored by the sample data
        out = []
        for t in self._threads:
            haystack = (t["subject"] + " " + " ".join(m["from"] + " " + m["to"] + " " + m["body"] for m in t["messages"])).lower()
            if all(term in haystack for term in terms):
                out.append(
                    {
                        "thread_id": t["id"],
                        "subject": t["subject"],
                        "messages": [
                            {"date": _day(m["days"]), "from": m["from"], "to": m["to"], "body": m["body"]}
                            for m in t["messages"]
                        ],
                    }
                )
        return out[:max_results]

    def create_draft(self, to: str, subject: str, body: str, thread_id: str = "") -> dict:
        return _record("email_draft", {"to": to, "subject": subject, "body": body, "thread_id": thread_id})

    def my_address(self) -> str:
        return "me"

    def send_to_self(self, subject: str, body_markdown: str) -> dict:
        return _record("email_to_self", {"subject": subject, "body": body_markdown})

    def upcoming_events(self, days_ahead: int = 14) -> list[dict]:
        return [
            {"id": e["id"], "title": e["title"], "start": f"{_day(e['days'])}T{e['time']}", "attendees": e["attendees"]}
            for e in self._events
            if 0 <= e["days"] <= days_ahead
        ]
