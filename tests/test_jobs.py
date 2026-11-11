"""Offline tests for the unattended jobs: brief scheduling, post-call follow-up, queued approvals, email watch."""

from __future__ import annotations

import json
import os
import plistlib
import tempfile
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path

import anthropic
import httpx2

os.environ["SALES_ASSISTANT_MOCK"] = "1"

from sales_assistant import jobs, schedule
from sales_assistant.agent import build_connectors
from sales_assistant.config import get_settings
from sales_assistant.connectors import mock
from sales_assistant.followups import find_followups
from sales_assistant.state import Store


def _message(content, stop_reason):
    return {
        "id": "msg_test", "type": "message", "role": "assistant", "model": "claude-opus-5-5", "content": content,
        "stop_reason": stop_reason, "stop_sequence": None, "usage": {"input_tokens": 1, "output_tokens": 1},
    }


def _tool(name, args, n=1):
    return {"type": "tool_use", "id": f"toolu_{n}", "name": name, "input": args}


class JobsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        mock.OUTBOX = root / "outbox.jsonl"
        self._root, jobs.ROOT = jobs.ROOT, root
        self.store = Store(root / "var")
        self.sent: list[dict] = []

    def tearDown(self):
        jobs.ROOT = self._root
        self.tmp.cleanup()

    def ctx(self, replies):
        replies = list(replies)

        def handler(request):
            self.sent.append(json.loads(request.content))
            return httpx2.Response(200, json=replies[len(self.sent) - 1])

        client = anthropic.Anthropic(
            api_key="test", base_url="http://stub.invalid", max_retries=0,
            http_client=anthropic.DefaultHttpxClient(transport=httpx2.MockTransport(handler)),
        )
        settings = get_settings()
        return jobs.Context(settings=settings, connectors=build_connectors(settings), store=self.store, client=client)

    def outbox(self):
        return [json.loads(line) for line in mock.OUTBOX.read_text().splitlines()] if mock.OUTBOX.exists() else []

    def test_email_watch_and_draft_dedupe(self):
        conn = build_connectors(get_settings())
        items = find_followups(conn, date.today())
        self.assertEqual([(i["type"], i["thread_id"], i["days_waiting"]) for i in items], [("reply_owed", "t-1", 5), ("no_reply", "t-2", 13)])
        self.assertFalse(items[0]["draft_already_saved"])
        self.store.record_draft("t-1")
        items = find_followups(conn, date.today(), drafted=self.store.drafted_threads())
        self.assertTrue(items[0]["draft_already_saved"])
        self.assertEqual(find_followups(conn, date.today(), reply_owed_days=6, no_reply_days=14), [])

    def test_brief_due_only_once_on_weekdays_after_the_set_time(self):
        ctx = self.ctx([])
        monday = datetime(2026, 11, 9, 7, 0)
        self.assertFalse(jobs.brief_is_due(ctx, monday))                      # before 07:40
        self.assertTrue(jobs.brief_is_due(ctx, monday.replace(hour=9)))
        self.assertFalse(jobs.brief_is_due(ctx, datetime(2026, 11, 8, 9, 0)))  # Sunday
        self.store.change(lambda s: s.__setitem__("last_brief", "2026-11-09"))
        self.assertFalse(jobs.brief_is_due(ctx, monday.replace(hour=9)))

    def test_run_due_follows_up_new_call_and_writes_brief(self):
        draft = {"to": "sofia.marchetti@halcyon-media.example", "subject": "Recap", "body": "Hi Sofia", "thread_id": ""}
        ctx = self.ctx([
            # post-call run for m-3 (the only call inside the first-run window)
            _message([_tool("create_email_draft", draft, 1), _tool("log_crm_note", {"deal_id": "d-104", "body": "Discovery call"}, 2),
                      _tool("create_crm_task", {"deal_id": "d-104", "title": "Send pilot scope", "due_date": "2026-11-16"}, 3)], "tool_use"),
            _message([{"type": "text", "text": "Recap drafted; note and task await approval."}], "end_turn"),
            # brief
            _message([_tool("email_followups", {}, 4)], "tool_use"),
            _message([{"type": "text", "text": "## Do today\\n\\n1. Reply to Tomas."}], "end_turn"),
        ])
        now = datetime.combine(date.today(), datetime.min.time()).replace(hour=9)
        produced = jobs.run_due(ctx, now, force_brief=True)
        self.assertEqual([u["kind"] for u in produced], ["post_call", "brief"])

        # The draft went straight to the mailbox; CRM writes are parked for approval, not performed.
        self.assertEqual([o["kind"] for o in self.outbox()], ["email_draft"])
        self.assertEqual([p["action"]["op"] for p in self.store.pending()], ["add_note", "create_task"])
        self.assertIn("Halcyon", self.store.pending()[0]["source"])
        result_text = json.dumps(self.sent[1]["messages"][-1])
        self.assertIn("Queued for the rep's approval", result_text)

        # The brief saw the email-watch data, was stored and saved to disk.
        self.assertIn("reply_owed", json.dumps(self.sent[3]["messages"][-1]))
        self.assertEqual(self.store.read()["last_brief"], date.today().isoformat())
        self.assertTrue((jobs.ROOT / "briefs" / f"{date.today().isoformat()}.md").exists())
        self.assertEqual(len(self.store.list_updates()), 2)

        # A second run does nothing: the call is marked done and the brief is not due again.
        calls_before = len(self.sent)
        self.assertEqual(jobs.run_due(ctx, now), [])
        self.assertEqual(len(self.sent), calls_before)

    def test_first_run_skips_old_calls_and_internal_meetings(self):
        ctx = self.ctx([])
        new = jobs.new_calls(ctx, date.today())
        self.assertEqual([m["id"] for m in new], ["m-3"])  # m-2 is two days old, m-1 is outside the window
        internal = {"id": "x", "title": "Team sync", "date": date.today().isoformat(), "attendees": [{"name": "A", "email": "a@mine.example"}]}
        ctx.settings.__dict__["rep_email"] = "rep@mine.example"
        self.assertIsNone(jobs.run_post_call(ctx, internal))
        self.assertEqual(self.sent, [])

    def test_brief_email_goes_only_to_self(self):
        ctx = self.ctx([_message([{"type": "text", "text": "All quiet."}], "end_turn")])
        jobs.run_brief(ctx, email=True, create_drafts=False)
        self.assertEqual([o["kind"] for o in self.outbox()], ["email_to_self"])

    def test_claude_failure_does_not_burn_call_attempts(self):
        ctx = self.ctx([])
        ctx.client = anthropic.Anthropic(
            api_key="bad", base_url="http://stub.invalid", max_retries=0,
            http_client=anthropic.DefaultHttpxClient(transport=httpx2.MockTransport(
                lambda r: httpx2.Response(401, json={"type": "error", "error": {"type": "authentication_error", "message": "bad key"}}))),
        )
        self.assertEqual(jobs.run_due(ctx, datetime.combine(date.today(), datetime.min.time()).replace(hour=9), force_brief=True), [])
        state = self.store.read()
        self.assertEqual(state.get("call_attempts", {}), {})
        self.assertNotIn("last_brief", state)

    def test_launchd_plist(self):
        data = plistlib.loads(schedule.plist_bytes(Path("/Users/x/sales-assistant"), "/Users/x/sales-assistant/.venv/bin/python"))
        self.assertEqual(data["ProgramArguments"][1:], ["-m", "sales_assistant", "run-jobs"])
        self.assertEqual(data["StartInterval"], 900)
        self.assertEqual(data["WorkingDirectory"], "/Users/x/sales-assistant")
        self.assertEqual(schedule.protected_folder(Path.home() / "Downloads" / "sales-assistant--main"), "Downloads")
        self.assertIsNone(schedule.protected_folder(Path.home() / "sales-assistant"))


if __name__ == "__main__":
    unittest.main()
