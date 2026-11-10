"""Fathom connector (external API v1: GET /meetings with X-Api-Key).

NOTE: written without access to Fathom's API reference (the docs host was not
reachable from the build environment). Field names are read defensively; run
`python -m sales_assistant doctor` once with a real key and adjust `_norm` if
anything comes back empty.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import requests

BASE = "https://api.fathom.ai/external/v1"
MAX_PAGES = 5


def _first(d: dict, *keys, default=None):
    for k in keys:
        if d.get(k) not in (None, "", []):
            return d[k]
    return default


class FathomMeetings:
    name = "Fathom"

    def __init__(self, api_key: str) -> None:
        self._http = requests.Session()
        self._http.headers.update({"X-Api-Key": api_key})

    def _list(self, params: dict) -> list[dict]:
        items: list[dict] = []
        cursor = None
        for _ in range(MAX_PAGES):
            query = dict(params)
            if cursor:
                query["cursor"] = cursor
            resp = self._http.get(f"{BASE}/meetings", params=query, timeout=60)
            if resp.status_code >= 400:
                raise RuntimeError(f"Fathom GET /meetings failed: {resp.status_code} {resp.text[:300]}")
            data = resp.json()
            items.extend(data.get("items", []))
            cursor = data.get("next_cursor")
            if not cursor:
                break
        return items

    @staticmethod
    def _id(m: dict) -> str:
        return str(_first(m, "recording_id", "id", "share_url", "url", default=""))

    def _norm(self, m: dict) -> dict:
        summary = m.get("default_summary") or {}
        if isinstance(summary, dict):
            summary = _first(summary, "markdown_formatted", "text", default="")
        return {
            "id": self._id(m),
            "title": _first(m, "meeting_title", "title", default=""),
            "date": str(_first(m, "recording_start_time", "scheduled_start_time", "created_at", default=""))[:10],
            "url": _first(m, "share_url", "url", default=""),
            "attendees": [
                {"name": a.get("name", ""), "email": a.get("email", "")} for a in m.get("calendar_invitees") or []
            ],
            "summary": summary,
            "action_items": [
                a.get("description", "") if isinstance(a, dict) else str(a) for a in m.get("action_items") or []
            ],
        }

    def list_meetings(self, domain: str = "", days_back: int = 30) -> list[dict]:
        since = datetime.now(timezone.utc) - timedelta(days=days_back)
        params: dict = {
            "created_after": since.isoformat(timespec="seconds"),
            "include_summary": "true",
            "include_action_items": "true",
        }
        if domain:
            params["calendar_invitees_domains[]"] = domain
        return [self._norm(m) for m in self._list(params)]

    def get_transcript(self, meeting_id: str) -> dict:
        # The list endpoint is the only documented way to get transcripts by API key, so scan recent meetings.
        since = datetime.now(timezone.utc) - timedelta(days=120)
        items = self._list({"created_after": since.isoformat(timespec="seconds"), "include_transcript": "true"})
        for m in items:
            if self._id(m) == meeting_id:
                lines = [
                    {
                        "speaker": (t.get("speaker") or {}).get("display_name", "") if isinstance(t.get("speaker"), dict) else str(t.get("speaker", "")),
                        "time": t.get("timestamp", ""),
                        "text": t.get("text", ""),
                    }
                    for t in m.get("transcript") or []
                ]
                base = self._norm(m)
                return {"id": base["id"], "title": base["title"], "date": base["date"], "transcript": lines}
        raise LookupError(f"No Fathom meeting with id {meeting_id!r} in the last 120 days")
