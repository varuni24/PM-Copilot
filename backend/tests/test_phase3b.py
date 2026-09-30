import json
import os
import time
import unittest
from unittest.mock import patch

import httpx
from mcp import Client
from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from app.llm import OllamaLLM
from mcp_servers.notion import mcp as notion_mcp
from mcp_servers.slack import _user_cache, mcp as slack_mcp


PAGE_ID = "b55c9c91-384d-452b-81db-d1ef79372b75"
SLACK_MATCH = {"text": "DS701 API integration deadline is Friday <@U123> <!channel> <@U999|someone>", "ts": "1757952000.000001", "user": "U123", "channel": {"id": "C123", "name": "ds701"}, "permalink": "https://example.slack.com/archives/C123/p1757952000000001"}
NOTION_PAGE = {"object": "page", "id": PAGE_ID, "url": "https://notion.so/DS701-project", "last_edited_time": "2026-09-15T12:00:00Z", "properties": {"Name": {"type": "title", "title": [{"plain_text": "DS701 Project"}]}, "Status": {"type": "status", "status": {"name": "In progress"}}}}
NOTION_SOURCE = {"object": "data_source", "id": "a42a62ed-9b51-4b98-9dea-ea6d091bc508", "title": [{"plain_text": "Project Tracker"}], "parent": {"database_id": "d9824bdc-8445-4327-be8b-5b47500af6ce"}, "url": "https://notion.so/tracker"}


def fake_database() -> MCPServer:
    server = MCPServer("Mock PostgreSQL")

    @server.tool()
    def get_overdue_tasks() -> dict[str, list[dict[str, str]]]:
        """Read overdue project tasks."""
        return {"tasks": [{"title": "Finish API documentation", "owner": "Sarah"}]}

    return server


def mock_slack_api(method, params=None, **kwargs):
    if method == "search.messages":
        return {"ok": True, "messages": {"matches": [{**SLACK_MATCH, "ts": str(time.time() - 60)}], "pagination": {"total_count": 1}}}
    if method == "conversations.list":
        return {"ok": True, "channels": [{"id": "C123", "name": "ds701", "is_private": False, "is_member": True}], "response_metadata": {"next_cursor": ""}}
    if method == "conversations.history":
        return {"ok": True, "messages": [{"text": "Latest DS701 update from <@U123>", "ts": "1757952100.000001", "user": "U123"}], "has_more": False}
    if method == "users.info":
        return {"ok": True, "user": {"id": "U123", "name": "test.user",
                                      "profile": {"display_name": "Test User", "real_name": "Test User Real Name"}}}
    raise AssertionError(method)


def mock_notion_request(method, path, **kwargs):
    if path == "/search":
        object_type = kwargs["body"]["filter"]["value"]
        return {"results": [NOTION_PAGE if object_type == "page" else NOTION_SOURCE], "has_more": False, "next_cursor": None}
    if path.startswith("/pages/"):
        return NOTION_PAGE
    if path.startswith("/blocks/"):
        return {"results": [{"id": "block-1", "type": "paragraph", "paragraph": {"rich_text": [{"plain_text": "The DS701 project is on track."}]}, "has_children": False}], "has_more": False}
    raise AssertionError(path)


class Phase3BTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        _user_cache.clear()

    async def test_slack_discovery_and_mocked_tools(self) -> None:
        with patch("mcp_servers.slack._api", side_effect=mock_slack_api):
            async with Client(slack_mcp) as client:
                tools = (await client.list_tools()).tools
                self.assertEqual({tool.name for tool in tools}, {"search_messages", "get_channel_history", "get_channels"})
                self.assertTrue(all(tool.description and tool.input_schema and tool.output_schema for tool in tools))
                found = await client.call_tool("search_messages", {"query": "DS701"})
                self.assertFalse(found.is_error)
                self.assertEqual(
                    found.structured_content["messages"][0]["text"],
                    "DS701 API integration deadline is Friday @Test User @channel @someone",
                )
                self.assertEqual(found.structured_content["messages"][0]["permalink"], SLACK_MATCH["permalink"])
                self.assertEqual(found.structured_content["messages"][0]["user_name"], "Test User")
                channels = await client.call_tool("get_channels", {})
                self.assertEqual(channels.structured_content["channels"][0]["name"], "ds701")
                history = await client.call_tool("get_channel_history", {"channel": "#ds701"})
                self.assertFalse(history.is_error)
                self.assertEqual(history.structured_content["messages"][0]["text"], "Latest DS701 update from @Test User")
                self.assertIsNone(history.structured_content["messages"][0]["permalink"])
                self.assertEqual(history.structured_content["messages"][0]["user_name"], "Test User")

    async def test_slack_user_name_falls_back_to_user_id_on_api_error(self) -> None:
        def api(method, params=None, **kwargs):
            if method == "users.info":
                raise ToolError("Slack API error: missing_scope")
            return mock_slack_api(method, params, **kwargs)

        with patch("mcp_servers.slack._api", side_effect=api):
            async with Client(slack_mcp) as client:
                result = await client.call_tool("search_messages", {"query": "DS701"})

        self.assertEqual(result.structured_content["messages"][0]["user_name"], "U123")

    async def test_slack_user_name_cache_prevents_duplicate_lookup(self) -> None:
        user_info_calls = 0

        def api(method, params=None, **kwargs):
            nonlocal user_info_calls
            if method == "users.info":
                user_info_calls += 1
            return mock_slack_api(method, params, **kwargs)

        with patch("mcp_servers.slack._api", side_effect=api):
            async with Client(slack_mcp) as client:
                first = await client.call_tool("search_messages", {"query": "DS701"})
                second = await client.call_tool("search_messages", {"query": "DS701"})

        self.assertEqual(first.structured_content["messages"][0]["user_name"], "Test User")
        self.assertEqual(second.structured_content["messages"][0]["user_name"], "Test User")
        self.assertEqual(user_info_calls, 1)

    async def test_slack_membership_archives_and_paginated_search(self) -> None:
        calls = []
        conversations = [
            {"id": "C123", "name": "joined", "is_member": True},
            {"id": "C999", "name": "not-joined", "is_member": False},
            {"id": "G123", "name": "private-joined", "is_private": True, "is_member": True},
            {"id": "G999", "name": "private-not-joined", "is_private": True, "is_member": False},
            {"id": "C777", "name": "archived", "is_member": True, "is_archived": True},
        ]

        def api(method, params=None, **kwargs):
            calls.append((method, params))
            if method == "conversations.list":
                return {"channels": conversations, "response_metadata": {"next_cursor": ""}}
            if method == "search.messages":
                page = params["page"]
                ids = ["C999", "G999", "C777"] if page == 1 else ["C123", "G123"]
                return {"messages": {"matches": [{**SLACK_MATCH, "channel": {"id": cid, "name": cid}} for cid in ids],
                                     "pagination": {"page_count": 2}}}
            if method == "conversations.history":
                return {"messages": [{"text": "joined only", "ts": "1.0"}], "has_more": False}
            raise AssertionError(method)

        with patch.dict(os.environ, {"SLACK_INCLUDE_DMS": ""}), patch("mcp_servers.slack._api", side_effect=api):
            async with Client(slack_mcp) as client:
                listed = await client.call_tool("get_channels", {})
                self.assertEqual({item["id"] for item in listed.structured_content["channels"]}, {"C123", "G123"})
                self.assertTrue(any(params["exclude_archived"] == "true" for method, params in calls if method == "conversations.list"))
                found = await client.call_tool("search_messages", {"query": "deadline"})
                self.assertEqual({item["channel_id"] for item in found.structured_content["messages"]}, {"C123", "G123"})
                self.assertEqual([params["page"] for method, params in calls if method == "search.messages"], [1, 2])
                one = await client.call_tool("search_messages", {"query": "deadline", "limit": 1})
                self.assertEqual([item["channel_id"] for item in one.structured_content["messages"]], ["C123"])
                self.assertTrue(one.structured_content["has_more"])
                for denied in ("C999", "G999", "C777"):
                    response = await client.call_tool("get_channel_history", {"channel": denied})
                    self.assertTrue(response.is_error)
                self.assertFalse((await client.call_tool("get_channel_history", {"channel": "G123"})).is_error)
                self.assertEqual(len([method for method, _ in calls if method == "conversations.history"]), 1)

    async def test_slack_dm_and_group_dm_access(self) -> None:
        calls = []

        def api(method, params=None, **kwargs):
            calls.append((method, params))
            if method == "conversations.list":
                conversations = [
                    {"id": "D123", "is_im": True, "user": "UOTHER"},  # Slack omits is_member for 1:1 DMs.
                    {"id": "D999", "is_im": True, "user": "UDENIED", "is_member": False},
                    {"id": "G456", "name": "team-group-dm", "is_mpim": True, "is_member": True},
                    {"id": "G999", "name": "other-group-dm", "is_mpim": True, "is_member": False},
                    {"id": "D777", "is_im": True, "user": "UARCHIVED", "is_archived": True},
                ]
                kind = params["types"]
                selected = [item for item in conversations if (kind == "im" and item.get("is_im")) or
                            (kind == "mpim" and item.get("is_mpim"))]
                return {"channels": selected, "response_metadata": {"next_cursor": ""}}
            if method == "search.messages":
                return {"messages": {"matches": [{**SLACK_MATCH, "channel": {"id": cid}} for cid in ("D123", "G456", "D999", "G999", "D777")],
                                     "pagination": {"page_count": 1}}}
            if method == "conversations.history":
                return {"messages": [{"text": "DM content", "ts": "2.0"}], "has_more": False}
            raise AssertionError(method)

        with patch.dict(os.environ, {"SLACK_INCLUDE_DMS": "true"}), patch("mcp_servers.slack._api", side_effect=api):
            async with Client(slack_mcp) as client:
                listed = await client.call_tool("get_channels", {})
                self.assertEqual({item["id"]: item["kind"] for item in listed.structured_content["channels"]},
                                 {"D123": "dm", "G456": "group_dm"})
                self.assertEqual({params["types"] for method, params in calls if method == "conversations.list"},
                                 {"public_channel,private_channel", "im", "mpim"})
                found = await client.call_tool("search_messages", {"query": "API in:<@UOTHER>"})
                self.assertEqual({item["channel_id"] for item in found.structured_content["messages"]}, {"D123", "G456"})
                for allowed in ("D123", "G456"):
                    self.assertFalse((await client.call_tool("get_channel_history", {"channel": allowed})).is_error)
                self.assertTrue((await client.call_tool("get_channel_history", {"channel": "D999"})).is_error)
                self.assertEqual([params["channel"] for method, params in calls if method == "conversations.history"], ["D123", "G456"])

    async def test_slack_missing_dm_scopes_do_not_break_channel_tools(self) -> None:
        calls = []

        def api(method, params=None, **kwargs):
            calls.append((method, params))
            if method == "conversations.list":
                if params["types"] in ("im", "mpim"):
                    raise ToolError("Slack API error: missing_scope.")
                self.assertEqual(params["types"], "public_channel,private_channel")
                self.assertEqual(params["limit"], 999)
                return {"channels": [{"id": "C123", "name": "ds701", "is_member": True},
                                     {"id": "C999", "name": "unjoined", "is_member": False}],
                        "response_metadata": {"next_cursor": ""}}
            if method == "search.messages":
                return {"messages": {"matches": [SLACK_MATCH, {**SLACK_MATCH, "channel": {"id": "C999"}}],
                                     "pagination": {"page_count": 1}}}
            raise AssertionError(method)

        with patch("mcp_servers.slack._api", side_effect=api):
            async with Client(slack_mcp) as client:
                with patch.dict(os.environ, {"SLACK_INCLUDE_DMS": ""}):
                    listed = await client.call_tool("get_channels", {})
                    found = await client.call_tool("search_messages", {"query": "DS701"})
                    self.assertEqual({params["types"] for method, params in calls if method == "conversations.list"},
                                     {"public_channel,private_channel"})
                self.assertEqual([item["id"] for item in listed.structured_content["channels"]], ["C123"])
                self.assertEqual([item["channel_id"] for item in found.structured_content["messages"]], ["C123"])
                with patch.dict(os.environ, {"SLACK_INCLUDE_DMS": "true"}):
                    listed = await client.call_tool("get_channels", {})
                    found = await client.call_tool("search_messages", {"query": "DS701"})
                self.assertFalse(listed.is_error)
                self.assertFalse(found.is_error)
                self.assertEqual([item["channel_id"] for item in found.structured_content["messages"]], ["C123"])
                self.assertIn("im", {params["types"] for method, params in calls if method == "conversations.list"})

    async def test_notion_discovery_and_mocked_tools(self) -> None:
        with patch("mcp_servers.notion._request", side_effect=mock_notion_request):
            async with Client(notion_mcp) as client:
                tools = (await client.list_tools()).tools
                self.assertEqual({tool.name for tool in tools}, {"search_pages", "get_page", "search_databases"})
                self.assertTrue(all(tool.description and tool.input_schema and tool.output_schema for tool in tools))
                found = await client.call_tool("search_pages", {"query": "DS701"})
                self.assertFalse(found.is_error)
                self.assertEqual(found.structured_content["pages"][0]["title"], "DS701 Project")
                databases = await client.call_tool("search_databases", {"query": "tracker"})
                self.assertEqual(databases.structured_content["data_sources"][0]["title"], "Project Tracker")
                page = await client.call_tool("get_page", {"page_id": PAGE_ID})
                self.assertFalse(page.is_error)
                self.assertEqual(page.structured_content["properties"]["Status"], "In progress")
                self.assertEqual(page.structured_content["blocks"][0]["text"], "The DS701 project is on track.")

    async def test_slack_notion_tool_routing_and_no_token_leak(self) -> None:
        postgres = fake_database()
        sources = {"memory://postgres": postgres, "memory://slack": slack_mcp, "memory://notion": notion_mcp}
        environment = {
            "MCP_URL": "memory://postgres", "CALENDAR_MCP_URL": "", "DRIVE_MCP_URL": "",
            "SLACK_MCP_URL": "memory://slack", "NOTION_MCP_URL": "memory://notion",
            "SLACK_USER_TOKEN": "test-slack-token-secret", "NOTION_API_TOKEN": "test-notion-token-secret",
        }

        async def run_case(question, names, expected_sources):
            requests = []

            def handler(request):
                body = json.loads(request.content)
                requests.append(body)
                serialized = json.dumps(body)
                self.assertNotIn("test-slack-token-secret", serialized)
                self.assertNotIn("test-notion-token-secret", serialized)
                if len(requests) == 1:
                    listed = {tool["function"]["name"] for tool in body["tools"]}
                    self.assertTrue(set(names).issubset(listed))
                    self.assertIn("Use Slack tools", body["messages"][0]["content"])
                    self.assertIn("Use Notion tools", body["messages"][0]["content"])
                    calls = [{"type": "function", "function": {"name": name, "arguments": {"query": "DS701"} if name in ("search_messages", "search_pages") else {}}} for name in names]
                    return httpx.Response(200, json={"message": {"role": "assistant", "content": "", "tool_calls": calls}})
                observed_sources = [json.loads(item["content"])["source"] for item in body["messages"] if item["role"] == "tool"]
                self.assertEqual(observed_sources[-len(expected_sources):], expected_sources)
                return httpx.Response(200, json={"message": {"role": "assistant", "content": "Answered from authorized sources."}})

            original = httpx.AsyncClient

            def mock_http(*args, **kwargs):
                kwargs["transport"] = httpx.MockTransport(handler)
                return original(*args, **kwargs)

            with patch.dict(os.environ, environment), patch("app.llm.Client", side_effect=lambda url: Client(sources[url])), patch("app.llm.httpx.AsyncClient", side_effect=mock_http), patch("mcp_servers.slack._api", side_effect=mock_slack_api), patch("mcp_servers.notion._request", side_effect=mock_notion_request):
                return await OllamaLLM().reply([{"role": "user", "content": question}])

        self.assertIn("Answered", await run_case("What was discussed in Slack about DS701?", ["search_messages"], ["Slack"]))
        self.assertIn("Answered", await run_case("Find the DS701 project page in Notion", ["search_pages"], ["Notion"]))
        self.assertIn("Answered", await run_case("What does Slack say about DS701 and what tasks are overdue?", ["search_messages", "get_overdue_tasks"], ["Slack", "PostgreSQL"]))
