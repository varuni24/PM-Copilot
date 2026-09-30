import asyncio
import json
import os
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo

import httpx
from googleapiclient.errors import HttpError
from mcp import Client
from mcp.server import MCPServer

from mcp_servers.calendar import mcp as calendar_mcp
from mcp_servers.drive import mcp as drive_mcp
from app.llm import OllamaLLM, _drive_question


CALENDAR_EVENT = {"id": "event-1", "summary": "Stakeholder sync", "start": {"dateTime": "2026-09-15T10:00:00-04:00"}, "end": {"dateTime": "2026-09-15T10:30:00-04:00"}, "htmlLink": "https://calendar.google.com/example"}
DRIVE_FILE = {"id": "file-1", "name": "Project requirements", "mimeType": "application/vnd.google-apps.document", "description": "Requirements document", "modifiedTime": "2026-09-14T12:00:00Z", "webViewLink": "https://drive.google.com/example"}


def fake_postgres_mcp() -> MCPServer:
    server = MCPServer("Mock PostgreSQL")

    @server.tool()
    def get_overdue_tasks() -> dict[str, list[dict[str, str]]]:
        """Read overdue tasks stored in PostgreSQL."""
        return {"tasks": [{"title": "Finish API documentation", "owner": "Sarah"}]}

    return server


class GoogleMCPTests(unittest.IsolatedAsyncioTestCase):
    async def test_missing_google_auth_keeps_google_tools_unavailable(self) -> None:
        postgres = fake_postgres_mcp()
        with tempfile.TemporaryDirectory() as directory:
            observed = []

            def handler(request):
                body = json.loads(request.content)
                observed.extend(tool["function"]["name"] for tool in body["tools"])
                self.assertIn("Google calendar is not authenticated", body["messages"][0]["content"])
                self.assertIn("Google drive is not authenticated", body["messages"][0]["content"])
                return httpx.Response(200, json={"message": {"role": "assistant", "content": "Google data is unavailable until OAuth authentication is complete."}})

            original = httpx.AsyncClient

            def mock_http(*args, **kwargs):
                kwargs["transport"] = httpx.MockTransport(handler)
                return original(*args, **kwargs)

            environment = {
                "MCP_URL": "memory://postgres",
                "CALENDAR_MCP_URL": "memory://calendar",
                "DRIVE_MCP_URL": "memory://drive",
                "GOOGLE_CALENDAR_TOKEN_FILE": str(Path(directory) / "missing-calendar.json"),
                "GOOGLE_DRIVE_TOKEN_FILE": str(Path(directory) / "missing-drive.json"),
            }
            with patch.dict(os.environ, environment), patch("app.llm.Client", side_effect=lambda url: Client(postgres)), patch("app.llm.httpx.AsyncClient", side_effect=mock_http):
                reply = await OllamaLLM().reply([{"role": "user", "content": "What meetings do I have today?"}])
            self.assertIn("unavailable", reply)
            self.assertNotIn("get_events_by_date", observed)
            self.assertNotIn("search_files", observed)

    async def test_calendar_discovery_and_mock_events(self) -> None:
        service = MagicMock()
        service.events.return_value.list.return_value.execute.return_value = {"items": [CALENDAR_EVENT]}
        with patch("mcp_servers.calendar._service", return_value=service):
            async with Client(calendar_mcp) as client:
                tools = (await client.list_tools()).tools
                self.assertEqual({tool.name for tool in tools}, {"get_upcoming_events", "get_events_by_date", "get_events_this_week"})
                self.assertTrue(all(tool.description and tool.input_schema and tool.output_schema for tool in tools))
                result = await client.call_tool("get_events_by_date", {"day": "2026-09-15"})
                self.assertFalse(result.is_error)
                self.assertEqual(result.structured_content["events"][0]["title"], "Stakeholder sync")
                self.assertEqual(result.structured_content["timezone"], "America/New_York")
                upcoming = await client.call_tool("get_upcoming_events", {"limit": 1})
                self.assertFalse(upcoming.is_error)
                self.assertEqual(len(upcoming.structured_content["events"]), 1)

    async def test_calendar_this_week_monday_sunday_boundary(self) -> None:
        service = MagicMock()
        sunday = {**CALENDAR_EVENT, "id": "sunday", "summary": "Sunday meeting", "start": {"dateTime": "2026-09-20T22:00:00-04:00"}}
        next_monday = {**CALENDAR_EVENT, "id": "next-monday", "summary": "Next week", "start": {"dateTime": "2026-09-21T09:00:00-04:00"}}
        service.events.return_value.list.return_value.execute.return_value = {"items": [CALENDAR_EVENT, sunday, next_monday]}
        with patch("mcp_servers.calendar._service", return_value=service), patch("mcp_servers.calendar.datetime", wraps=datetime) as clock:
            clock.now.return_value = datetime(2026, 9, 15, 12, tzinfo=ZoneInfo("America/New_York"))
            async with Client(calendar_mcp) as client:
                result = await client.call_tool("get_events_this_week", {})
        self.assertFalse(result.is_error)
        self.assertEqual(result.structured_content["week_start"], "2026-09-14")
        self.assertEqual(result.structured_content["week_end"], "2026-09-20")
        self.assertEqual({event["title"] for event in result.structured_content["events"]}, {"Stakeholder sync", "Sunday meeting"})
        arguments = service.events.return_value.list.call_args.kwargs
        self.assertEqual(arguments["timeMin"], "2026-09-14T00:00:00-04:00")
        self.assertEqual(arguments["timeMax"], "2026-09-21T00:00:00-04:00")

    async def test_calendar_tomorrow_after_week_answer_uses_september_16_events(self) -> None:
        service = MagicMock()
        lydia = {**CALENDAR_EVENT, "id": "lydia-1", "summary": "Meet with Lydia", "start": {"dateTime": "2026-09-16T12:15:00-04:00"}, "end": {"dateTime": "2026-09-16T13:00:00-04:00"}}
        check_in = {**CALENDAR_EVENT, "id": "lydia-2", "summary": "Lydia & Varuni Check-in", "start": {"dateTime": "2026-09-16T12:30:00-04:00"}, "end": {"dateTime": "2026-09-16T13:00:00-04:00"}}
        service.events.return_value.list.return_value.execute.return_value = {"items": [lydia, check_in]}

        def handler(request):
            return httpx.Response(200, json={"message": {"role": "assistant", "content": "No meetings are scheduled for tomorrow, September 16, 2026."}})

        original = httpx.AsyncClient

        def mock_http(*args, **kwargs):
            kwargs["transport"] = httpx.MockTransport(handler)
            return original(*args, **kwargs)

        history = [
            {"role": "user", "content": "What meetings do I have this week?"},
            {"role": "assistant", "content": "Meet with Lydia and Lydia & Varuni Check-in are on September 16."},
            {"role": "user", "content": "Do I have any meeting tomorrow?"},
        ]
        with patch("mcp_servers.calendar._service", return_value=service), patch("app.llm.httpx.AsyncClient", side_effect=mock_http), patch("app.llm.datetime", wraps=datetime) as clock:
            clock.now.return_value = datetime(2026, 9, 15, 12, tzinfo=ZoneInfo("America/New_York"))
            async with Client(calendar_mcp) as calendar:
                tools = (await calendar.list_tools()).tools
                reply = await OllamaLLM()._reply_with_tools(history, {tool.name: calendar for tool in tools}, {tool.name: "Google Calendar" for tool in tools}, tools, [])
        self.assertIn("2026-09-16", reply)
        self.assertIn("Meet with Lydia", reply)
        self.assertIn("Lydia & Varuni Check-in", reply)
        self.assertIn("12:15 PM–1:00 PM", reply)
        self.assertNotIn("No meetings are scheduled", reply)
        arguments = service.events.return_value.list.call_args.kwargs
        self.assertEqual(arguments["timeMin"], "2026-09-16T00:00:00-04:00")
        self.assertEqual(arguments["timeMax"], "2026-09-17T00:00:00-04:00")

    async def test_calendar_wrong_qwen_tomorrow_date_is_corrected(self) -> None:
        service = MagicMock()
        service.events.return_value.list.return_value.execute.return_value = {"items": [{**CALENDAR_EVENT, "start": {"dateTime": "2026-09-16T12:15:00-04:00"}}]}
        calls = 0

        def handler(request):
            nonlocal calls
            calls += 1
            if calls == 1:
                return httpx.Response(200, json={"message": {"role": "assistant", "content": "", "tool_calls": [{"type": "function", "function": {"name": "get_events_by_date", "arguments": {"day": "2026-09-17"}}}]}})
            return httpx.Response(200, json={"message": {"role": "assistant", "content": "No meetings tomorrow."}})

        original = httpx.AsyncClient

        def mock_http(*args, **kwargs):
            kwargs["transport"] = httpx.MockTransport(handler)
            return original(*args, **kwargs)

        with patch("mcp_servers.calendar._service", return_value=service), patch("app.llm.httpx.AsyncClient", side_effect=mock_http), patch("app.llm.datetime", wraps=datetime) as clock:
            clock.now.return_value = datetime(2026, 9, 15, 12, tzinfo=ZoneInfo("America/New_York"))
            async with Client(calendar_mcp) as calendar:
                tools = (await calendar.list_tools()).tools
                reply = await OllamaLLM()._reply_with_tools([{"role": "user", "content": "What meetings do I have tomorrow?"}], {tool.name: calendar for tool in tools}, {tool.name: "Google Calendar" for tool in tools}, tools, [])
        self.assertIn("Stakeholder sync", reply)
        self.assertIn("2026-09-16", reply)
        self.assertEqual(service.events.return_value.list.call_args.kwargs["timeMin"], "2026-09-16T00:00:00-04:00")

    async def test_drive_discovery_and_mock_files(self) -> None:
        service = MagicMock()
        service.files.return_value.list.return_value.execute.return_value = {"files": [DRIVE_FILE]}
        service.files.return_value.get.return_value.execute.return_value = {**DRIVE_FILE, "capabilities": {"canDownload": True}}
        service.files.return_value.export.return_value.execute.return_value = b"Project requirements content"
        with patch("mcp_servers.drive._service", return_value=service):
            async with Client(drive_mcp) as client:
                tools = (await client.list_tools()).tools
                self.assertEqual({tool.name for tool in tools}, {"search_files", "get_file"})
                self.assertTrue(all(tool.description and tool.input_schema and tool.output_schema for tool in tools))
                search_tool = next(tool for tool in tools if tool.name == "search_files")
                self.assertEqual(search_tool.input_schema["properties"]["limit"]["default"], 20)
                found = await client.call_tool("search_files", {"query": "project"})
                self.assertFalse(found.is_error)
                self.assertEqual(found.structured_content["files"][0]["name"], "Project requirements")
                file = await client.call_tool("get_file", {"file_id": "file-1"})
                self.assertFalse(file.is_error)
                self.assertEqual(file.structured_content["content"], "Project requirements content")

    async def test_drive_course_code_variants_and_pages(self) -> None:
        service = MagicMock()
        spaced = {**DRIVE_FILE, "id": "spaced", "name": "FA26 - DS 701 Projects"}
        compact = {**DRIVE_FILE, "id": "compact", "name": "ds701 notes"}
        service.files.return_value.list.return_value.execute.side_effect = [
            {"files": [spaced], "nextPageToken": "next"},
            {"files": [compact]},
        ]
        with patch("mcp_servers.drive._service", return_value=service):
            async with Client(drive_mcp) as client:
                result = await client.call_tool("search_files", {"query": "DS 701"})
        self.assertFalse(result.is_error)
        self.assertEqual({file["name"] for file in result.structured_content["files"]}, {"FA26 - DS 701 Projects", "ds701 notes"})
        self.assertFalse(result.structured_content["has_more"])
        first_query = service.files.return_value.list.call_args_list[0].kwargs["q"]
        self.assertIn("fullText contains '\"DS 701\"'", first_query)
        self.assertIn("fullText contains 'DS701'", first_query)
        self.assertIn("fullText contains 'ds701'", first_query)
        self.assertEqual(service.files.return_value.list.call_args_list[0].kwargs["corpora"], "allDrives")
        self.assertTrue(service.files.return_value.list.call_args_list[0].kwargs["includeItemsFromAllDrives"])
        self.assertTrue(service.files.return_value.list.call_args_list[0].kwargs["supportsAllDrives"])
        self.assertEqual(service.files.return_value.list.call_args_list[1].kwargs["pageToken"], "next")

    async def test_drive_natural_query_stop_words_or_joining_and_quoted_phrases(self) -> None:
        cases = (
            ("Can you access the project description for the WGBH project from drive?", ["WGBH", "project", "description"]),
            ("WGBH project description", ["WGBH", "project", "description"]),
        )
        for query, terms in cases:
            with self.subTest(query=query):
                service = MagicMock()
                service.files.return_value.list.return_value.execute.return_value = {"files": []}
                with patch("mcp_servers.drive._service", return_value=service):
                    async with Client(drive_mcp) as client:
                        await client.call_tool("search_files", {"query": query})
                drive_query = service.files.return_value.list.call_args.kwargs["q"]
                for term in terms:
                    self.assertIn(f"fullText contains '{term}'", drive_query)
                self.assertIn(" or ", drive_query)
                for stop_word in ("can", "you", "the", "for", "from", "access"):
                    self.assertNotIn(f"fullText contains '{stop_word}'", drive_query)

        service = MagicMock()
        service.files.return_value.list.return_value.execute.return_value = {"files": []}
        with patch("mcp_servers.drive._service", return_value=service):
            async with Client(drive_mcp) as client:
                await client.call_tool("search_files", {"query": 'WGBH "project description"'})
        quoted_query = service.files.return_value.list.call_args.kwargs["q"]
        self.assertIn("fullText contains '\"project description\"'", quoted_query)
        self.assertIn("fullText contains 'WGBH'", quoted_query)
        self.assertIn(" or ", quoted_query)

    async def test_drive_intent_default_limit_and_relevance_ordering(self) -> None:
        self.assertTrue(_drive_question("Find the WGBH project description"))
        self.assertTrue(_drive_question("Get me the project charter"))

        service = MagicMock()
        service.files.return_value.list.return_value.execute.return_value = {"files": []}
        with patch("mcp_servers.drive._service", return_value=service):
            async with Client(drive_mcp) as client:
                await client.call_tool("search_files", {"query": "Find me all files from drive on WGBH"})
        searched = service.files.return_value.list.call_args.kwargs
        self.assertNotIn("fullText contains 'all'", searched["q"])
        self.assertIn("fullText contains 'WGBH'", searched["q"])
        self.assertEqual(searched["pageSize"], 20)
        self.assertEqual(searched["orderBy"], "relevance desc")

        service = MagicMock()
        service.files.return_value.list.return_value.execute.return_value = {"files": []}
        with patch("mcp_servers.drive._service", return_value=service):
            async with Client(drive_mcp) as client:
                await client.call_tool("search_files", {})
        recent = service.files.return_value.list.call_args.kwargs
        self.assertEqual(recent["pageSize"], 20)
        self.assertEqual(recent["orderBy"], "modifiedTime desc")

    async def test_drive_relevance_order_falls_back_on_http_400(self) -> None:
        service = MagicMock()
        error_response = MagicMock(status=400, reason="Bad Request")
        service.files.return_value.list.return_value.execute.side_effect = [
            HttpError(error_response, b'{"error":{"message":"Invalid orderBy"}}'),
            {"files": [DRIVE_FILE]},
        ]
        with patch("mcp_servers.drive._service", return_value=service):
            async with Client(drive_mcp) as client:
                result = await client.call_tool("search_files", {"query": "WGBH project description"})
        self.assertFalse(result.is_error)
        self.assertEqual(result.structured_content["files"][0]["name"], "Project requirements")
        calls = service.files.return_value.list.call_args_list
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0].kwargs["orderBy"], "relevance desc")
        self.assertEqual(calls[1].kwargs["orderBy"], "modifiedTime desc")

    async def test_drive_results_are_locally_ranked_by_filename_then_modified_time(self) -> None:
        async def ranked_names(query, files):
            service = MagicMock()
            service.files.return_value.list.return_value.execute.return_value = {"files": files}
            with patch("mcp_servers.drive._service", return_value=service):
                async with Client(drive_mcp) as client:
                    result = await client.call_tool("search_files", {"query": query})
            return [item["name"] for item in result.structured_content["files"]]

        wgbh = {**DRIVE_FILE, "id": "wgbh", "name": "WGBH Team Agreement", "modifiedTime": "2026-09-01T12:00:00Z"}
        template = {**DRIVE_FILE, "id": "template", "name": "Team Agreement Template", "modifiedTime": "2026-10-02T12:00:00Z"}
        self.assertEqual(
            await ranked_names("Find the WGBH project description", [template, wgbh]),
            ["WGBH Team Agreement", "Team Agreement Template"],
        )

        generic_kickoff = {**DRIVE_FILE, "id": "generic", "name": "(Oct 2) Client Kickoff Meeting", "modifiedTime": "2026-10-02T12:00:00Z"}
        wgbh_kickoff = {**DRIVE_FILE, "id": "specific", "name": "WGBH Client Kickoff Meeting", "modifiedTime": "2026-09-01T12:00:00Z"}
        self.assertEqual(
            await ranked_names("WGBH Kickoff Meeting", [generic_kickoff, wgbh_kickoff]),
            ["WGBH Client Kickoff Meeting", "(Oct 2) Client Kickoff Meeting"],
        )

        older = {**DRIVE_FILE, "id": "older", "name": "Weekly Meeting Notes", "modifiedTime": "2026-09-01T12:00:00Z"}
        newer = {**DRIVE_FILE, "id": "newer", "name": "Client Meeting Notes", "modifiedTime": "2026-10-02T12:00:00Z"}
        self.assertEqual(
            await ranked_names("meeting", [older, newer]),
            ["Client Meeting Notes", "Weekly Meeting Notes"],
        )

    async def test_drive_general_requests_and_file_types(self) -> None:
        cases = [
            ("DS701", None, ["fullText contains 'DS701'", "fullText contains '\"DS 701\"'"]),
            ("Find my files related to project management", None, ["fullText contains 'project'", "fullText contains 'management'"]),
            ("What all resume files I have?", None, ["fullText contains 'resume'"]),
            ("resume", "document", ["fullText contains 'resume'"]),
            ("Are there any PPTs?", None, ["mimeType = 'application/vnd.google-apps.presentation'", "mimeType = 'application/vnd.ms-powerpoint'", "mimeType = 'application/vnd.openxmlformats-officedocument.presentationml.presentation'"]),
            ("PowerPoint files", None, ["mimeType = 'application/vnd.google-apps.presentation'", "mimeType = 'application/vnd.ms-powerpoint'"]),
            ("is there any DS594 Presentations", None, ["fullText contains 'DS594'", "mimeType = 'application/vnd.google-apps.presentation'"]),
            ("DS594", None, ["fullText contains 'DS594'"]),
            (None, "presentation", ["mimeType = 'application/vnd.google-apps.presentation'"]),
        ]
        for query, file_type, expected in cases:
            with self.subTest(query=query, file_type=file_type):
                service = MagicMock()
                service.files.return_value.list.return_value.execute.return_value = {"files": []}
                arguments = {"query": query, "file_type": file_type}
                with patch("mcp_servers.drive._service", return_value=service):
                    async with Client(drive_mcp) as client:
                        result = await client.call_tool("search_files", arguments)
                self.assertFalse(result.is_error)
                drive_query = service.files.return_value.list.call_args.kwargs["q"]
                for clause in expected:
                    self.assertIn(clause, drive_query)
                if query == "resume":
                    self.assertNotIn("mimeType", drive_query)
                self.assertEqual(service.files.return_value.list.call_args.kwargs["corpora"], "allDrives")

    async def test_drive_final_answer_uses_only_tool_files_and_links(self) -> None:
        real_files = [
            {**DRIVE_FILE, "id": "slide-1", "name": "DS594 Kickoff", "mimeType": "application/vnd.google-apps.presentation", "webViewLink": "https://docs.google.com/presentation/d/slide-1/edit"},
            {**DRIVE_FILE, "id": "slide-2", "name": "DS594 Project Slides", "mimeType": "application/vnd.google-apps.presentation", "webViewLink": "https://docs.google.com/presentation/d/slide-2/edit"},
        ]
        service = MagicMock()
        service.files.return_value.list.return_value.execute.side_effect = [
            {"files": [real_files[0]], "nextPageToken": "next"},
            {"files": [real_files[1]]},
        ]
        requests = []

        def handler(request):
            body = json.loads(request.content)
            requests.append(body)
            if len(requests) == 1:
                return httpx.Response(200, json={"message": {"role": "assistant", "content": "", "tool_calls": [{"type": "function", "function": {"name": "search_files", "arguments": {"query": "DS594", "file_type": "folder", "limit": 1}}}]}})
            return httpx.Response(200, json={"message": {"role": "assistant", "content": "Found DS594 Presentations folder and Fake DS594 Slides: https://drive.google.com/fake-link"}})

        original = httpx.AsyncClient

        def mock_http(*args, **kwargs):
            kwargs["transport"] = httpx.MockTransport(handler)
            return original(*args, **kwargs)

        with patch("mcp_servers.drive._service", return_value=service), patch("app.llm.httpx.AsyncClient", side_effect=mock_http):
            async with Client(drive_mcp) as drive:
                tools = (await drive.list_tools()).tools
                reply = await OllamaLLM()._reply_with_tools(
                    [{"role": "user", "content": "is there any DS594 Presentations"}],
                    {tool.name: drive for tool in tools},
                    {tool.name: "Google Drive" for tool in tools},
                    tools,
                    [],
                )
        self.assertIn("DS594 Kickoff", reply)
        self.assertIn("DS594 Project Slides", reply)
        self.assertIn("https://docs.google.com/presentation/d/slide-1/edit", reply)
        self.assertIn("https://docs.google.com/presentation/d/slide-2/edit", reply)
        self.assertNotIn("DS594 Presentations folder", reply)
        self.assertNotIn("Fake DS594 Slides", reply)
        self.assertNotIn("fake-link", reply)
        self.assertEqual(len(service.files.return_value.list.call_args_list), 2)
        self.assertEqual(service.files.return_value.list.call_args_list[0].kwargs["pageSize"], 20)
        self.assertIn("mimeType = 'application/vnd.google-apps.presentation'", service.files.return_value.list.call_args_list[0].kwargs["q"])

    async def test_drive_zero_results_cannot_be_replaced_with_suggestions(self) -> None:
        service = MagicMock()
        service.files.return_value.list.return_value.execute.return_value = {"files": []}
        calls = 0

        def handler(request):
            nonlocal calls
            calls += 1
            if calls == 1:
                return httpx.Response(200, json={"message": {"role": "assistant", "content": "", "tool_calls": [{"type": "function", "function": {"name": "search_files", "arguments": {"query": "DS594 presentations"}}}]}})
            return httpx.Response(200, json={"message": {"role": "assistant", "content": "Try the DS594 Presentations folder at https://drive.google.com/fake-link"}})

        original = httpx.AsyncClient

        def mock_http(*args, **kwargs):
            kwargs["transport"] = httpx.MockTransport(handler)
            return original(*args, **kwargs)

        with patch("mcp_servers.drive._service", return_value=service), patch("app.llm.httpx.AsyncClient", side_effect=mock_http):
            async with Client(drive_mcp) as drive:
                tools = (await drive.list_tools()).tools
                reply = await OllamaLLM()._reply_with_tools(
                    [{"role": "user", "content": "DS594 presentations"}],
                    {tool.name: drive for tool in tools},
                    {tool.name: "Google Drive" for tool in tools},
                    tools,
                    [],
                )
        self.assertEqual(reply, "No Google Drive files matched the search.")
        self.assertNotIn("fake-link", reply)

    async def test_drive_file_claim_without_tool_call_triggers_real_search(self) -> None:
        service = MagicMock()
        service.files.return_value.list.return_value.execute.return_value = {"files": [{**DRIVE_FILE, "id": "real", "name": "DS594 Real Slides", "mimeType": "application/vnd.google-apps.presentation", "webViewLink": "https://docs.google.com/presentation/d/real/edit"}]}

        def handler(request):
            return httpx.Response(200, json={"message": {"role": "assistant", "content": "I found Fake DS594 Presentations folder at https://drive.google.com/fake-link"}})

        original = httpx.AsyncClient

        def mock_http(*args, **kwargs):
            kwargs["transport"] = httpx.MockTransport(handler)
            return original(*args, **kwargs)

        with patch("mcp_servers.drive._service", return_value=service), patch("app.llm.httpx.AsyncClient", side_effect=mock_http):
            async with Client(drive_mcp) as drive:
                tools = (await drive.list_tools()).tools
                reply = await OllamaLLM()._reply_with_tools(
                    [{"role": "user", "content": "is there any DS594 Presentations"}],
                    {tool.name: drive for tool in tools},
                    {tool.name: "Google Drive" for tool in tools},
                    tools,
                    [],
                )
        self.assertIn("DS594 Real Slides", reply)
        self.assertIn("https://docs.google.com/presentation/d/real/edit", reply)
        self.assertNotIn("Fake DS594 Presentations folder", reply)
        self.assertNotIn("fake-link", reply)
        self.assertTrue(service.files.return_value.list.called)

    async def test_unavailable_drive_cannot_claim_files(self) -> None:
        postgres = fake_postgres_mcp()

        def handler(request):
            return httpx.Response(200, json={"message": {"role": "assistant", "content": "I found a DS594 folder at https://drive.google.com/fake-link"}})

        original = httpx.AsyncClient

        def mock_http(*args, **kwargs):
            kwargs["transport"] = httpx.MockTransport(handler)
            return original(*args, **kwargs)

        with patch("app.llm.httpx.AsyncClient", side_effect=mock_http):
            async with Client(postgres) as database:
                tools = (await database.list_tools()).tools
                reply = await OllamaLLM()._reply_with_tools(
                    [{"role": "user", "content": "is there any DS594 Presentations"}],
                    {tool.name: database for tool in tools},
                    {tool.name: "PostgreSQL" for tool in tools},
                    tools,
                    ["Google drive integration is not configured."],
                )
        self.assertIn("unavailable", reply)
        self.assertNotIn("fake-link", reply)

    async def test_qwen_tool_routing_calendar_drive_and_combined(self) -> None:
        postgres = fake_postgres_mcp()
        with tempfile.TemporaryDirectory() as directory:
            calendar_token = Path(directory) / "calendar.json"
            drive_token = Path(directory) / "drive.json"
            calendar_token.write_text("{}")
            drive_token.write_text("{}")
            environment = {
                "MCP_URL": "memory://postgres",
                "CALENDAR_MCP_URL": "memory://calendar",
                "DRIVE_MCP_URL": "memory://drive",
                "GOOGLE_CALENDAR_TOKEN_FILE": str(calendar_token),
                "GOOGLE_DRIVE_TOKEN_FILE": str(drive_token),
            }

            def client_for(url):
                return Client({"memory://postgres": postgres, "memory://calendar": calendar_mcp, "memory://drive": drive_mcp}[url])

            async def run_case(question, chosen_tools, expected_sources):
                requests = []

                def handler(request):
                    body = json.loads(request.content)
                    requests.append(body)
                    if len(requests) == 1:
                        names = [tool["function"]["name"] for tool in body["tools"]]
                        self.assertTrue(all(name in names for name in chosen_tools))
                        prompt = body["messages"][0]["content"]
                        self.assertIn("Drive search available: yes", prompt)
                        self.assertIn("get_events_this_week", prompt)
                        self.assertIn("resumes", prompt)
                        return httpx.Response(200, json={"message": {"role": "assistant", "content": "", "tool_calls": [{"type": "function", "function": {"name": name, "arguments": {"day": "2026-09-15"} if name == "get_events_by_date" else {"query": question} if name == "search_files" else {}}} for name in chosen_tools]}})
                    source_labels = [json.loads(message["content"])["source"] for message in body["messages"] if message["role"] == "tool"]
                    self.assertEqual(source_labels, expected_sources)
                    return httpx.Response(200, json={"message": {"role": "assistant", "content": "Answered using the selected sources."}})

                original = httpx.AsyncClient

                def mock_http(*args, **kwargs):
                    kwargs["transport"] = httpx.MockTransport(handler)
                    return original(*args, **kwargs)

                with patch.dict(os.environ, environment), patch("app.llm.Client", side_effect=client_for), patch("app.llm.httpx.AsyncClient", side_effect=mock_http):
                    return await OllamaLLM().reply([{"role": "user", "content": question}])

            calendar_service = MagicMock()
            calendar_service.events.return_value.list.return_value.execute.return_value = {"items": [CALENDAR_EVENT]}
            drive_service = MagicMock()
            drive_service.files.return_value.list.return_value.execute.return_value = {"files": [DRIVE_FILE]}
            with patch("mcp_servers.calendar._service", return_value=calendar_service), patch("mcp_servers.drive._service", return_value=drive_service):
                self.assertIn("Stakeholder sync", await run_case("What meetings do I have today?", ["get_events_by_date"], ["Google Calendar"]))
                self.assertIn("Project requirements", await run_case("Find project requirements in Drive", ["search_files"], ["Google Drive"]))
                self.assertIn("Google Drive returned", await run_case("Find the WGBH project description", ["search_files"], ["Google Drive"]))
                self.assertIn("Google Drive returned", await run_case("Get me the project charter", ["search_files"], ["Google Drive"]))
                self.assertIn("Google Drive returned", await run_case("What all resume files I have?", ["search_files"], ["Google Drive"]))
                self.assertIn("Google Drive returned", await run_case("Are there any PPTs?", ["search_files"], ["Google Drive"]))
                self.assertIn("Google Drive returned", await run_case("Find my files related to project management", ["search_files"], ["Google Drive"]))
                self.assertIn("Answered", await run_case("What meetings do I have this week?", ["get_events_this_week"], ["Google Calendar"]))
                self.assertIn("Answered", await run_case("What meetings today and what tasks are overdue?", ["get_events_by_date", "get_overdue_tasks"], ["Google Calendar", "PostgreSQL"]))


if __name__ == "__main__":
    unittest.main()
