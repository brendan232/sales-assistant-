"""Offline tests: sample-data tools, the approval gate, and the agent loop against a stubbed Claude API.

Run with:  python -m unittest discover -s tests -v
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

import anthropic
import httpx2

os.environ["SALES_ASSISTANT_MOCK"] = "1"

from sales_assistant import agent as agent_mod
from sales_assistant.config import get_settings
from sales_assistant.connectors import mock
from sales_assistant.signals import deal_flags
from sales_assistant.tools import Approver, build_tools


def _tool(tools, name):
    return next(t for t in tools if getattr(t, "name", None) == name)


class SignalsTest(unittest.TestCase):
    def test_flags(self):
        today = date(2026, 6, 1)
        deal = {
            "last_activity_at": (today - timedelta(days=15)).isoformat(),
            "contacts": [{"name": "A", "title": "Analyst"}],
            "close_date": (today - timedelta(days=2)).isoformat(),
            "next_step": "",
        }
        flags = {f["flag"] for f in deal_flags(deal, today, has_upcoming_meeting=False)}
        self.assertEqual(
            flags,
            {"stalled", "single_threaded", "no_senior_contact", "close_date_passed", "no_next_step", "no_meeting_booked"},
        )

    def test_healthy_deal_has_no_flags(self):
        today = date(2026, 6, 1)
        deal = {
            "last_activity_at": today.isoformat(),
            "contacts": [{"name": "A", "title": "VP Sales"}, {"name": "B", "title": "Engineer"}],
            "close_date": (today + timedelta(days=40)).isoformat(),
            "next_step": "Demo",
        }
        self.assertEqual(deal_flags(deal, today, has_upcoming_meeting=True), [])


class ToolsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        mock.OUTBOX = Path(self.tmp.name) / "outbox.jsonl"
        self.conn = agent_mod.build_connectors(get_settings())

    def tearDown(self):
        self.tmp.cleanup()

    def test_pipeline_review_ranks_riskiest_first(self):
        tools = build_tools(self.conn, Approver("deny"))
        data = json.loads(_tool(tools, "pipeline_review").call({}))
        deals = data["open_deals"]
        self.assertEqual(len(deals), 4)
        self.assertEqual(deals[0]["id"], "d-101")  # single-threaded, quiet, no next step, no meeting
        flags = {f["flag"] for f in deals[0]["flags"]}
        self.assertIn("single_threaded", flags)
        self.assertIn("no_meeting_booked", flags)
        brightline = next(d for d in deals if d["id"] == "d-102")
        self.assertNotIn("no_meeting_booked", {f["flag"] for f in brightline["flags"]})

    def test_reads(self):
        tools = build_tools(self.conn, Approver("deny"))
        deal = json.loads(_tool(tools, "get_deal").call({"deal_id": "d-101"}))
        self.assertIn("CFO", deal["notes"][0]["body"])
        calls = json.loads(_tool(tools, "list_meetings").call({"company_domain": "northwind-logistics.example"}))
        self.assertEqual([c["id"] for c in calls], ["m-1"])
        transcript = json.loads(_tool(tools, "get_meeting_transcript").call({"meeting_id": "m-1"}))
        self.assertTrue(any("Greg" in line["text"] for line in transcript["transcript"]))
        threads = json.loads(_tool(tools, "search_email").call({"query": "koto-robotics.example"}))
        self.assertEqual(threads[0]["thread_id"], "t-1")

    def test_unknown_deal_is_a_tool_error(self):
        from anthropic.lib.tools import ToolError

        tools = build_tools(self.conn, Approver("deny"))
        with self.assertRaises(ToolError):
            _tool(tools, "get_deal").call({"deal_id": "nope"})

    def test_writes_are_gated(self):
        args = {"deal_id": "d-101", "body": "hello"}
        denied = _tool(build_tools(self.conn, Approver("deny")), "log_crm_note").call(args)
        self.assertIn("Not performed", denied)
        self.assertFalse(mock.OUTBOX.exists())

        declined = _tool(build_tools(self.conn, Approver("ask", prompt=lambda _: "n")), "log_crm_note").call(args)
        self.assertIn("declined", declined)
        self.assertFalse(mock.OUTBOX.exists())

        _tool(build_tools(self.conn, Approver("ask", prompt=lambda _: "y")), "log_crm_note").call(args)
        self.assertEqual(json.loads(mock.OUTBOX.read_text())["kind"], "crm_note")

    def test_drafts_allowed_in_unattended_mode_only_when_enabled(self):
        args = {"to": "a@b.example", "subject": "s", "body": "b"}
        tools = build_tools(self.conn, Approver("deny", allow_drafts=True))
        self.assertIn("created", _tool(tools, "create_email_draft").call(args))
        self.assertIn("Not performed", _tool(tools, "log_crm_note").call({"deal_id": "d-101", "body": "x"}))


def _message(content, stop_reason):
    return {
        "id": "msg_test",
        "type": "message",
        "role": "assistant",
        "model": agent_mod.MODEL,
        "content": content,
        "stop_reason": stop_reason,
        "stop_sequence": None,
        "usage": {"input_tokens": 10, "output_tokens": 10},
    }


class AgentLoopTest(unittest.TestCase):
    def test_tool_round_trip_and_history(self):
        requests = []
        replies = [
            _message(
                [
                    {"type": "text", "text": "Checking the pipeline."},
                    {"type": "tool_use", "id": "toolu_1", "name": "pipeline_review", "input": {}},
                ],
                "tool_use",
            ),
            _message([{"type": "text", "text": "Northwind needs you today."}], "end_turn"),
            _message([{"type": "text", "text": "Draft below."}], "end_turn"),
        ]

        def handler(request: httpx2.Request) -> httpx2.Response:
            requests.append(json.loads(request.content))
            return httpx2.Response(200, json=replies[len(requests) - 1])

        client = anthropic.Anthropic(
            api_key="test", base_url="http://stub.invalid", max_retries=0,
            http_client=anthropic.DefaultHttpxClient(transport=httpx2.MockTransport(handler)),
        )
        settings = get_settings()
        agent = agent_mod.SalesAgent(
            settings, agent_mod.build_connectors(settings), Approver("deny"), client=client, on_progress=lambda _: None
        )

        answer = agent.ask("What should I work on today?")
        self.assertEqual(answer, "Checking the pipeline.\nNorthwind needs you today.")

        first, second = requests
        self.assertEqual(first["model"], "claude-opus-5-5")
        self.assertEqual(first["fallbacks"], "default")
        self.assertEqual(first["output_config"], {"effort": "high"})
        self.assertNotIn("thinking", first)
        self.assertNotIn("thread", first)
        self.assertTrue(first["messages"][0]["content"].startswith("Today is "))
        self.assertIn("web_search", [t["name"] for t in first["tools"]])
        self.assertEqual(first["system"][0]["cache_control"], {"type": "ephemeral"})

        # The second request carries the tool result, and the tool actually ran on sample data.
        result = second["messages"][-1]["content"][0]
        self.assertEqual(result["type"], "tool_result")
        self.assertEqual(result["tool_use_id"], "toolu_1")
        result_text = result["content"] if isinstance(result["content"], str) else result["content"][0]["text"]
        self.assertIn("Northwind", result_text)

        # Our own history mirrors what was sent, so a follow-up turn replays it.
        self.assertEqual([m["role"] for m in agent.messages], ["user", "assistant", "user", "assistant"])
        agent.ask("Draft the follow-up.")
        third = requests[2]
        self.assertEqual([m["role"] for m in third["messages"]], ["user", "assistant", "user", "assistant", "user"])
        self.assertEqual(third["messages"][-1]["content"], "Draft the follow-up.")
        self.assertEqual(third["system"], first["system"])  # stable prefix -> cache hits


if __name__ == "__main__":
    unittest.main()
