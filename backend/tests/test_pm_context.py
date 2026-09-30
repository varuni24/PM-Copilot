import json
import os
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

import httpx
from mcp import Client
from mcp.server import MCPServer

from app.llm import OllamaLLM
from integrations.project_context import in_period, resolve_slack_context, slack_search_query


NOW = datetime(2026, 9, 15, 15, 0, tzinfo=ZoneInfo("America/New_York"))


class ProjectContextTests(unittest.IsolatedAsyncioTestCase):
    def test_configured_project_names_override_course_and_default_context(self) -> None:
        cases = (
            ("What happened in WGBH this week?", "WGBH Municipal Lawsuits", "i-fa26-ds701-wgbh-municipal-lawsuits"),
            ("Any updates on Humanitarian AI?", "Humanitarian AI Today", "i-fa26-ds701-humanitarian-ai-iati"),
            ("Summarize recent messages in BCAN", "BCAN Boston Heating", "i-fa26-ds701-bcan-heating"),
            ("What happened in DS701 WGBH this week?", "WGBH Municipal Lawsuits", "i-fa26-ds701-wgbh-municipal-lawsuits"),
        )
        for question, project, channel in cases:
            with self.subTest(question=question):
                context = resolve_slack_context(question, NOW)
                self.assertEqual(context.label, f"DS701 / {project}")
                self.assertEqual(context.channels, (channel,))
                self.assertTrue(context.explicit)

        general = resolve_slack_context("What happened in DS701 this week?", NOW)
        self.assertEqual(general.channels, ("i-fa26-ds701-pm-tpm",))
        default = resolve_slack_context("What happened in Slack this week?", NOW)
        self.assertEqual(default.channels, ("i-fa26-ds701-pm-tpm",))

    def test_ambiguous_partial_project_name_requires_clarification(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "context.json"
            path.write_text(json.dumps({"active_course": "ds701", "courses": {"ds701": {
                "code": "DS701", "slack": {"general_channels": ["general-pm"], "projects": {
                    "Climate Research": {"channels": ["climate-research"]},
                    "Climate Outreach": {"channels": ["climate-outreach"]},
                }}}}}))
            with patch.dict(os.environ, {"PM_CONTEXT_FILE": str(path)}):
                context = resolve_slack_context("Any Slack updates on Climate?", NOW)
            self.assertEqual(context.channels, ())
            self.assertEqual(context.clarification, ("Climate Research", "Climate Outreach"))

    async def test_ambiguous_project_response_asks_which_project(self) -> None:
        postgres = MCPServer("Mock PostgreSQL")
        slack = MCPServer("Mock Slack")

        @slack.tool()
        def search_messages(query: str, limit: int = 20) -> dict:
            """Search messages in Slack."""
            self.fail("Ambiguous project context must not search a channel")

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "context.json"
            path.write_text(json.dumps({"active_course": "ds701", "courses": {"ds701": {
                "code": "DS701", "slack": {"general_channels": ["general-pm"], "projects": {
                    "Climate Research": {"channels": ["climate-research"]},
                    "Climate Outreach": {"channels": ["climate-outreach"]},
                }}}}}))
            sources = {"memory://postgres": postgres, "memory://slack": slack}
            environment = {"PM_CONTEXT_FILE": str(path), "MCP_URL": "memory://postgres", "SLACK_MCP_URL": "memory://slack",
                           "SLACK_USER_TOKEN": "test-token", "CALENDAR_MCP_URL": "", "DRIVE_MCP_URL": "",
                           "NOTION_MCP_URL": ""}
            with patch.dict(os.environ, environment), patch("app.llm.Client", side_effect=lambda url: Client(sources[url])):
                answer = await OllamaLLM().reply([{"role": "user", "content": "Any Slack updates on Climate?"}])
        self.assertIn("Which project do you mean?", answer)
        self.assertIn("Climate Research", answer)
        self.assertIn("Climate Outreach", answer)

    def test_default_explicit_and_time_periods(self) -> None:
        default = resolve_slack_context("What happened in Slack this week?", NOW)
        self.assertEqual(default.label, "DS701")
        self.assertEqual(default.channels, ("i-fa26-ds701-pm-tpm",))
        self.assertEqual((default.start.date().isoformat(), default.end.date().isoformat()),
                         ("2026-09-14", "2026-09-21"))
        self.assertFalse(default.explicit)
        explicit = resolve_slack_context("Summarize #other-pm messages from the past week", NOW)
        self.assertEqual(explicit.channels, ("other-pm",))
        self.assertTrue(explicit.explicit)
        self.assertIn("in:#other-pm", slack_search_query(explicit.channels[0], explicit))
        unknown = resolve_slack_context("What happened in DS594 Slack this week?", NOW)
        self.assertEqual(unknown.channels, ())
        for request, start, end in (
            ("today", "2026-09-15", "2026-09-16"),
            ("yesterday", "2026-09-14", "2026-09-15"),
            ("this week", "2026-09-14", "2026-09-21"),
        ):
            context = resolve_slack_context(f"Slack messages {request}", NOW)
            self.assertEqual((context.start.date().isoformat(), context.end.date().isoformat()), (start, end))
        for request in ("past week", "past 7 days", "recently"):
            context = resolve_slack_context(f"Slack messages {request}", NOW)
            self.assertEqual(context.start, NOW.replace(day=8))
            self.assertEqual(context.end, NOW)

    def test_future_project_mapping_uses_config_not_slack_tool_logic(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "context.json"
            path.write_text(json.dumps({"active_course": "ds701", "courses": {"ds701": {
                "code": "DS701", "slack": {"general_channels": ["general-pm"],
                                           "projects": {"Project A": {"channels": ["project-a-private"]}}}}}}))
            with patch.dict(os.environ, {"PM_CONTEXT_FILE": str(path)}):
                general = resolve_slack_context("What happened in Slack today?", NOW)
                project = resolve_slack_context("What happened in Project A Slack today?", NOW)
            self.assertEqual(general.channels, ("general-pm",))
            self.assertEqual(project.channels, ("project-a-private",))

    async def test_backend_prefetches_multiple_bounded_messages_for_qwen(self) -> None:
        postgres = MCPServer("Mock PostgreSQL")
        slack = MCPServer("Mock Slack")
        searched = []

        @slack.tool()
        def search_messages(query: str, limit: int = 20) -> dict:
            """Search messages in joined Slack channels."""
            searched.append((query, limit))
            current = datetime.now(ZoneInfo("America/New_York"))
            inside = current.timestamp() - 120
            old = (current.replace(year=current.year - 1)).timestamp()
            return {"messages": [
                {"text": "API deadline moved to Friday", "timestamp": str(inside), "user_id": "U1", "channel_id": "C1", "channel_name": "pm", "permalink": "https://slack.test/1"},
                {"text": "Sarah will update stakeholders", "timestamp": str(inside - 60), "user_id": "U2", "channel_id": "C1", "channel_name": "pm", "permalink": "https://slack.test/2"},
                {"text": "Old unrelated message", "timestamp": str(old), "user_id": "U3", "channel_id": "C1", "channel_name": "pm", "permalink": "https://slack.test/3"},
            ], "has_more": False}

        sources = {"memory://postgres": postgres, "memory://slack": slack}
        requests = []
        expected = {"context": "DS701", "channel": "i-fa26-ds701-pm-tpm"}

        def handler(request):
            body = json.loads(request.content)
            requests.append(body)
            tool_results = [json.loads(item["content"]) for item in body["messages"] if item["role"] == "tool"]
            self.assertEqual(len(tool_results), 1)
            self.assertEqual([item["text"] for item in tool_results[0]["messages"]],
                             ["API deadline moved to Friday", "Sarah will update stakeholders"])
            self.assertEqual(tool_results[0]["context"], expected["context"])
            self.assertEqual(tool_results[0]["channel"], expected["channel"])
            return httpx.Response(200, json={"message": {"role": "assistant", "content":
                "The deadline moved to Friday, and Sarah will update stakeholders."}})

        original = httpx.AsyncClient

        def mock_http(*args, **kwargs):
            kwargs["transport"] = httpx.MockTransport(handler)
            return original(*args, **kwargs)

        environment = {"MCP_URL": "memory://postgres", "SLACK_MCP_URL": "memory://slack",
                       "SLACK_USER_TOKEN": "test-token", "CALENDAR_MCP_URL": "", "DRIVE_MCP_URL": "",
                       "NOTION_MCP_URL": ""}
        with patch.dict(os.environ, environment), patch("app.llm.Client", side_effect=lambda url: Client(sources[url])), \
             patch("app.llm.httpx.AsyncClient", side_effect=mock_http):
            answer = await OllamaLLM().reply([{"role": "user", "content":
                                               "Summarize the important Slack messages from the past week."}])
            expected.update(context="other-pm", channel="other-pm")
            override = await OllamaLLM().reply([{"role": "user", "content":
                                                 "Summarize #other-pm Slack messages from the past week."}])
        self.assertIn("deadline moved", answer)
        self.assertIn("deadline moved", override)
        self.assertEqual(len(searched), 2)
        self.assertIn("in:#i-fa26-ds701-pm-tpm", searched[0][0])
        self.assertIn("in:#other-pm", searched[1][0])
        self.assertIn("after:", searched[0][0])
        self.assertIn("before:", searched[0][0])
        self.assertEqual(searched[0][1], 100)
        self.assertEqual(len(requests), 2)
