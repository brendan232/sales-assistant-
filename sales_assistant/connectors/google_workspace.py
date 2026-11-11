"""Gmail + Google Calendar connector.

Scopes are deliberately narrow: read mail, compose, read calendar. Emails to
buyers are only ever saved as drafts. The one thing this module sends is the
rep's own brief, to the rep's own address (send_to_self). The first run opens a browser for OAuth consent and stores the token
in GOOGLE_TOKEN_FILE.
"""

from __future__ import annotations

import base64
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from pathlib import Path

SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.compose",
    "https://www.googleapis.com/auth/calendar.readonly",
]
BODY_LIMIT = 3000


def _credentials(client_secrets: Path, token_file: Path):
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow

    creds = Credentials.from_authorized_user_file(str(token_file), SCOPES) if token_file.exists() else None
    if creds and creds.valid:
        return creds
    if creds and creds.expired and creds.refresh_token:
        creds.refresh(Request())
    else:
        creds = InstalledAppFlow.from_client_secrets_file(str(client_secrets), SCOPES).run_local_server(port=0)
    token_file.write_text(creds.to_json())
    token_file.chmod(0o600)
    return creds


def _plain_text(payload: dict) -> str:
    """Depth-first search for the first text/plain part of a Gmail message payload."""
    if payload.get("mimeType") == "text/plain" and payload.get("body", {}).get("data"):
        return base64.urlsafe_b64decode(payload["body"]["data"]).decode("utf-8", "replace")
    for part in payload.get("parts") or []:
        text = _plain_text(part)
        if text:
            return text
    return ""


class GoogleWorkspace:
    name = "Gmail + Calendar"

    def __init__(self, client_secrets: Path, token_file: Path) -> None:
        from googleapiclient.discovery import build

        creds = _credentials(client_secrets, token_file)
        self._gmail = build("gmail", "v1", credentials=creds, cache_discovery=False)
        self._calendar = build("calendar", "v3", credentials=creds, cache_discovery=False)
        self._me: str | None = None

    def search_threads(self, query: str, max_results: int = 10) -> list[dict]:
        listing = self._gmail.users().threads().list(userId="me", q=query, maxResults=max_results).execute()
        out = []
        for ref in listing.get("threads", []):
            thread = self._gmail.users().threads().get(userId="me", id=ref["id"], format="full").execute()
            messages = []
            subject = ""
            for msg in thread.get("messages", [])[-6:]:
                headers = {h["name"].lower(): h["value"] for h in msg["payload"].get("headers", [])}
                subject = subject or headers.get("subject", "")
                body = _plain_text(msg["payload"]) or msg.get("snippet", "")
                messages.append(
                    {
                        "date": datetime.fromtimestamp(int(msg.get("internalDate", 0)) / 1000, timezone.utc).isoformat(timespec="seconds"),
                        "from": headers.get("from", ""),
                        "to": headers.get("to", ""),
                        "body": body[:BODY_LIMIT],
                    }
                )
            out.append({"thread_id": thread["id"], "subject": subject, "messages": messages})
        return out

    def create_draft(self, to: str, subject: str, body: str, thread_id: str = "") -> dict:
        mime = EmailMessage()
        mime["To"] = to
        mime["Subject"] = subject
        mime.set_content(body)
        message: dict = {"raw": base64.urlsafe_b64encode(mime.as_bytes()).decode()}
        if thread_id:
            message["threadId"] = thread_id
        draft = self._gmail.users().drafts().create(userId="me", body={"message": message}).execute()
        return {"status": "draft saved in Gmail (not sent)", "id": draft.get("id")}

    def my_address(self) -> str:
        if self._me is None:
            self._me = self._gmail.users().getProfile(userId="me").execute().get("emailAddress", "")
        return self._me

    def send_to_self(self, subject: str, body_markdown: str) -> dict:
        """Email the rep their own brief. The recipient is fixed to the signed-in address: this cannot mail anyone else."""
        import markdown

        mime = EmailMessage()
        mime["To"] = self.my_address()
        mime["Subject"] = subject
        mime.set_content(body_markdown)
        html = markdown.markdown(body_markdown, extensions=["tables", "sane_lists"])
        mime.add_alternative(
            f'<div style="font:15px/1.5 -apple-system,Segoe UI,Helvetica,Arial,sans-serif;max-width:680px">{html}</div>',
            subtype="html",
        )
        raw = base64.urlsafe_b64encode(mime.as_bytes()).decode()
        sent = self._gmail.users().messages().send(userId="me", body={"raw": raw}).execute()
        return {"status": "sent to self", "id": sent.get("id")}

    def upcoming_events(self, days_ahead: int = 14) -> list[dict]:
        now = datetime.now(timezone.utc)
        listing = (
            self._calendar.events()
            .list(
                calendarId="primary",
                timeMin=now.isoformat(),
                timeMax=(now + timedelta(days=days_ahead)).isoformat(),
                singleEvents=True,
                orderBy="startTime",
                maxResults=100,
            )
            .execute()
        )
        return [
            {
                "id": e.get("id"),
                "title": e.get("summary", ""),
                "start": e.get("start", {}).get("dateTime") or e.get("start", {}).get("date"),
                "attendees": [a.get("email", "") for a in e.get("attendees", [])],
            }
            for e in listing.get("items", [])
        ]
