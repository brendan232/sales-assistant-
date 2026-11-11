"""Offline tests for the web interface: pipeline API, a chat turn, and the approval round trip."""

from __future__ import annotations

import json
import os
import tempfile
import time
import unittest
from pathlib import Path

import anthropic
import httpx2

os.environ["SALES_ASSISTANT_MOCK"] = "1"

from sales_assistant.config import get_settings
from sales_assistant.connectors import mock
from sales_assistant.state import Store
from sales_assistant.web import create_app


def _message(content, stop_reason):
    return {
        "id": "msg_test", "type": "message", "role": "assistant", "model": "claude-opus-5-5", "content": content,
        "stop_reason": stop_reason, "stop_sequence": None, "usage": {"input_tokens": 1, "output_tokens": 1},
    }


def stub_client(replies):
    sent = []

    def handler(request):
        sent.append(json.loads(request.content))
        return httpx2.Response(200, json=replies[len(sent) - 1])

    return anthropic.Anthropic(
        api_key="test", base_url="http://stub.invalid", max_retries=0,
        http_client=anthropic.DefaultHttpxClient(transport=httpx2.MockTransport(handler)),
    )


class WebTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        mock.OUTBOX = Path(self.tmp.name) / "outbox.jsonl"

    def tearDown(self):
        self.tmp.cleanup()

    def app(self, replies=()):
        self.store = Store(Path(self.tmp.name) / "var")
        return create_app(get_settings(), client=stub_client(list(replies)), store=self.store).test_client()

    def wait(self, http, predicate, timeout=5.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            data = http.get("/api/events").get_json()
            if predicate(data):
                return data
            time.sleep(0.02)
        self.fail(f"timed out; last state: {data}")

    def test_page_status_and_pipeline(self):
        http = self.app()
        self.assertIn(b"Sales assistant", http.get("/").data)
        self.assertTrue(all(c["sample"] for c in http.get("/api/status").get_json()["connectors"]))
        deals = http.get("/api/pipeline").get_json()["open_deals"]
        self.assertEqual(deals[0]["id"], "d-101")

    def test_rejects_cross_site_and_non_json_posts(self):
        http = self.app()
        self.assertEqual(http.post("/api/chat", json={"text": "hi"}, headers={"Origin": "https://evil.example"}).status_code, 403)
        self.assertEqual(http.post("/api/chat", data="text=hi").status_code, 415)
        self.assertEqual(http.get("/api/status", headers={"Host": "evil.example"}).status_code, 403)

    def test_chat_turn_with_approval(self):
        draft = {"to": "priya@northwind-logistics.example", "subject": "Security pack", "body": "Hi Priya"}
        http = self.app([
            _message([{"type": "tool_use", "id": "toolu_1", "name": "create_email_draft", "input": draft}], "tool_use"),
            _message([{"type": "text", "text": "Draft saved."}], "end_turn"),
        ])
        self.assertEqual(http.post("/api/chat", json={"text": "Draft the follow-up"}).status_code, 200)
        self.assertEqual(http.post("/api/chat", json={"text": "again"}).status_code, 409)  # one turn at a time

        data = self.wait(http, lambda d: any(e["type"] == "approval" for e in d["events"]))
        approval = next(e for e in data["events"] if e["type"] == "approval")
        self.assertIn("Hi Priya", approval["preview"])
        self.assertFalse(mock.OUTBOX.exists())  # nothing written before the rep answers

        self.assertEqual(http.post("/api/approval", json={"id": approval["id"], "approve": True}).status_code, 200)
        data = self.wait(http, lambda d: not d["busy"])
        self.assertEqual([e["type"] for e in data["events"]], ["user", "progress", "approval", "approval_resolved", "answer"])
        self.assertEqual(data["events"][-1]["text"], "Draft saved.")
        self.assertEqual(json.loads(mock.OUTBOX.read_text())["kind"], "email_draft")

        self.assertEqual(http.post("/api/reset", json={}).status_code, 200)
        self.assertEqual(http.get("/api/events").get_json()["events"], [])

    def test_inbox_followups_and_deferred_approval(self):
        http = self.app()
        kinds = {(f["type"], f["thread_id"]) for f in http.get("/api/followups").get_json()["items"]}
        self.assertEqual(kinds, {("reply_owed", "t-1"), ("no_reply", "t-2")})

        update = self.store.add_update("brief", "Pipeline brief", "## Do today\n\nCall Priya.")
        action_id = self.store.queue_action("CRM note", "Deal d-101\n\nCall summary", {"op": "add_note", "args": {"deal_id": "d-101", "body": "Call summary"}}, "Call: Northwind")
        inbox = http.get("/api/inbox").get_json()
        self.assertEqual([u["id"] for u in inbox["updates"]], [update["id"]])
        self.assertFalse(inbox["updates"][0]["read"])
        self.assertEqual(inbox["pending"][0]["id"], action_id)

        self.assertEqual(http.post(f"/api/updates/{update['id']}/open", json={}).status_code, 200)
        events = http.get("/api/events").get_json()["events"]
        self.assertEqual(events[-1]["type"], "update")
        self.assertTrue(http.get("/api/inbox").get_json()["updates"][0]["read"])

        self.assertFalse(mock.OUTBOX.exists())
        self.assertEqual(http.post(f"/api/pending/{action_id}", json={"approve": True}).status_code, 200)
        self.assertEqual(json.loads(mock.OUTBOX.read_text())["kind"], "crm_note")
        self.assertEqual(http.post(f"/api/pending/{action_id}", json={"approve": True}).status_code, 404)  # only once
        self.assertEqual(http.get("/api/inbox").get_json()["pending"], [])

    def test_declined_approval_writes_nothing(self):
        http = self.app([
            _message([{"type": "tool_use", "id": "toolu_1", "name": "log_crm_note", "input": {"deal_id": "d-101", "body": "x"}}], "tool_use"),
            _message([{"type": "text", "text": "Okay, not logged."}], "end_turn"),
        ])
        http.post("/api/chat", json={"text": "log it"})
        data = self.wait(http, lambda d: any(e["type"] == "approval" for e in d["events"]))
        approval = next(e for e in data["events"] if e["type"] == "approval")
        http.post("/api/approval", json={"id": approval["id"], "approve": False})
        self.wait(http, lambda d: not d["busy"])
        self.assertFalse(mock.OUTBOX.exists())

    def test_missing_credentials_show_a_readable_error(self):
        saved = {k: os.environ.pop(k, None) for k in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")}
        try:
            http = create_app(get_settings(), store=Store(Path(self.tmp.name) / "var")).test_client()
            http.post("/api/chat", json={"text": "hi"})
            data = self.wait(http, lambda d: not d["busy"] and d["events"])
            self.assertEqual(data["events"][-1]["type"], "error")
        finally:
            os.environ.update({k: v for k, v in saved.items() if v is not None})


if __name__ == "__main__":
    unittest.main()
