import os
import json
import re
from contextlib import AsyncExitStack
from datetime import datetime, timedelta
from typing import Protocol
from zoneinfo import ZoneInfo

import httpx
from mcp import Client

from integrations.google_oauth import token_path
from mcp_servers.drive_grounding import render_drive_results
from integrations.project_context import in_period, resolve_slack_context, slack_search_query


SYSTEM_PROMPT = (
    "You are a helpful project management copilot. Give clear, practical "
    "answers. Ask for missing context when needed. Do not claim access to "
    "information beyond the tools provided for this request."
)


def _drive_question(question: str) -> bool:
    if re.search(r"\b(slack|notion)\b", question, re.IGNORECASE):
        return False
    return bool(re.search(
        r"\b(files?|folders?|documents?|presentations?|powerpoints?|ppts?|pdfs?|resumes?|drive|"
        r"descriptions?|specs?|specifications?|charters?|plans?|reports?|notes?|memos?|proposals?|"
        r"summaries|summary|briefs?|agendas?|minutes|docs?|docx|sheets?|spreadsheets?|decks?|"
        r"whitepapers?|contracts?|agreements?)\b",
        question,
        re.IGNORECASE,
    ))


def _requested_file_type(question: str) -> str | None:
    if re.search(r"\b(ppts?|powerpoints?|presentations?|slides?)\b", question, re.IGNORECASE):
        return "presentation"
    if re.search(r"\bpdfs?\b", question, re.IGNORECASE):
        return "pdf"
    if re.search(r"\bfolders?\b", question, re.IGNORECASE):
        return "folder"
    return None


def _calendar_day_request(question: str) -> tuple[str, str] | None:
    if (_drive_question(question) or re.search(r"\b(slack|notion|tasks?|projects?|people|decisions?|risks?)\b", question, re.IGNORECASE)
            or not re.search(r"\b(meetings?|events?|calendar)\b", question, re.IGNORECASE)):
        return None
    today = datetime.now(ZoneInfo("America/New_York")).date()
    if re.search(r"\btomorrow\b", question, re.IGNORECASE):
        return "tomorrow", (today + timedelta(days=1)).isoformat()
    if re.search(r"\btoday\b", question, re.IGNORECASE):
        return "today", today.isoformat()
    return None


def _render_calendar_day(label: str, day: str, data: dict) -> str:
    timezone = ZoneInfo("America/New_York")
    events = data.get("events", [])
    meetings = [event for event in events if "T" in event["start"]]
    all_day = [event for event in events if "T" not in event["start"]]
    noun = "meeting" if len(meetings) == 1 else "meetings"
    lines = [f"You have {len(meetings)} {noun} {label}, {day} (America/New_York)."]
    for event in meetings:
        start = datetime.fromisoformat(event["start"]).astimezone(timezone)
        end = datetime.fromisoformat(event["end"]).astimezone(timezone) if event.get("end") else None
        time = start.strftime("%I:%M %p").lstrip("0")
        if end:
            time += "–" + end.strftime("%I:%M %p").lstrip("0")
        lines.append(f"- {event['title']} — {time}" + (f"; location: {event['location']}" if event.get("location") else ""))
    for event in all_day:
        lines.append(f"- All-day event: {event['title']}")
    return "\n".join(lines)


class LLMError(Exception):
    """A model provider could not return a usable reply."""


class LLM(Protocol):
    async def reply(self, messages: list[dict[str, str]]) -> str:
        """Return the assistant's text reply for a conversation."""


class OllamaLLM:
    async def reply(self, messages: list[dict[str, str]]) -> str:
        mcp_url = os.getenv("MCP_URL", "http://127.0.0.1:8001/mcp")
        try:
            async with AsyncExitStack() as stack:
                database_client = await stack.enter_async_context(Client(mcp_url))
                available_tools = (await database_client.list_tools()).tools
                tool_clients = {tool.name: database_client for tool in available_tools}
                tool_sources = {tool.name: "PostgreSQL" for tool in available_tools}
                unavailable = []

                for source, env_name in (("calendar", "CALENDAR_MCP_URL"), ("drive", "DRIVE_MCP_URL"),
                                         ("slack", "SLACK_MCP_URL"), ("notion", "NOTION_MCP_URL")):
                    label = f"Google {source}" if source in ("calendar", "drive") else source.title()
                    url = os.getenv(env_name)
                    if not url:
                        unavailable.append(f"{label} integration is not configured.")
                        continue
                    if source in ("calendar", "drive") and not token_path(source).exists():
                        unavailable.append(f"{label} is not authenticated.")
                        continue
                    if source in ("slack", "notion") and not os.getenv("SLACK_USER_TOKEN" if source == "slack" else "NOTION_API_TOKEN"):
                        unavailable.append(f"{label} is not authenticated.")
                        continue
                    try:
                        google_client = await stack.enter_async_context(Client(url))
                        google_tools = (await google_client.list_tools()).tools
                    except Exception:
                        unavailable.append(f"{label} MCP server is unavailable.")
                        continue
                    for tool in google_tools:
                        if tool.name in tool_clients:
                            raise LLMError(f"Duplicate MCP tool name: {tool.name}")
                        available_tools.append(tool)
                        tool_clients[tool.name] = google_client
                        tool_sources[tool.name] = f"Google {source.title()}" if source in ("calendar", "drive") else source.title()

                return await self._reply_with_tools(messages, tool_clients, tool_sources, available_tools, unavailable)
        except LLMError:
            raise
        except Exception as exc:
            raise LLMError("Could not connect to the project tools. Start the MCP server and try again.") from exc

    async def _reply_with_tools(self, messages: list[dict[str, str]], tool_clients, tool_sources, available_tools, unavailable) -> str:
        model = os.getenv("OLLAMA_MODEL", "qwen3:4b-instruct")
        url = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434").rstrip("/")
        tool_names = {tool.name for tool in available_tools}
        drive_available = "search_files" in tool_names and tool_sources.get("search_files") == "Google Drive"
        latest_question = messages[-1]["content"]
        slack_context = resolve_slack_context(latest_question)
        ollama_tools = [
            {
                "type": "function",
                "function": {
                    "name": tool.name,
                    "description": tool.description or "",
                    "parameters": tool.input_schema,
                },
            }
            for tool in available_tools
        ]
        conversation = [
            {
                "role": "system",
                "content": (
                    f"{SYSTEM_PROMPT} Today in America/New_York is {datetime.now(ZoneInfo('America/New_York')).date().isoformat()}. "
                    "Use the available tools for questions about stored projects, tasks, "
                    "people, decisions, or risks, and for creating tasks. Never invent "
                    "database facts or claim a task was created unless a tool confirms it. "
                    "Use PostgreSQL tools for stored project-management data, Google "
                    "Calendar tools for meetings and events. For 'this week', call "
                    "get_events_this_week, which uses Monday through Sunday in "
                    "America/New_York; do not use get_upcoming_events for that. "
                    "For 'today' or 'tomorrow' meetings/events, call "
                    "get_events_by_date with that date in America/New_York. "
                    "Never answer from earlier chat history without a fresh "
                    "Calendar tool result; get_upcoming_events is not a day query. "
                    "Use Google Drive search_files for any request about files, "
                    "documents, folders, presentations, PPTs, PDFs, resumes, or "
                    "project-management content, even when the user does not say "
                    "'Google Drive'. Pass concise search terms or a natural request; "
                    "set file_type only for a specific format the user explicitly "
                    "requests, such as PPT/PowerPoint or PDF. For generic files, "
                    "resumes, or project-management files, omit file_type so PDFs, "
                    "presentations, documents, and other types can all appear. "
                    f"Drive search available: {'yes' if drive_available else 'no'}. "
                    "When Drive search is available, call it before answering a "
                    "file question and never claim Drive access is unavailable. "
                    "For Drive answers, use only the exact filenames, MIME types, "
                    "descriptions, dates, IDs, and URLs returned by Drive tools. "
                    "Never invent a folder or link. If a Drive search returns zero "
                    "files, say there are no matching results; do not suggest "
                    "specific files as if they were retrieved. Do not pass a small "
                    "limit for broad searches; respect has_more and incomplete_search. "
                    "Use multiple tools for questions spanning sources. "
                    "Use Slack tools for Slack messages, discussions, and channels; "
                    "search_messages finds discussions, get_channels finds channel "
                    "IDs, and get_channel_history reads recent channel messages. "
                    "For a Slack question without an explicit course/project/channel, "
                    "use the active PM context configured by the backend. An explicit "
                    "channel or configured project overrides that default. When the "
                    "backend has already supplied time-bounded Slack tool results, "
                    "synthesize all supplied messages, not just the first one; do not "
                    "claim a complete summary if has_more is true. Do not infer who "
                    "a person is from an ambiguous name or claim access to DMs. "
                    "Use Notion tools for Notion pages and project trackers; "
                    "search_pages and search_databases match titles only, then "
                    "get_page reads a returned page's properties and blocks. "
                    "Do not treat PostgreSQL tasks as Slack messages or Drive "
                    "files as Notion pages. Never invent Slack or Notion items, "
                    "quotes, URLs, or content; use only returned tool results. "
                    "If Slack or Notion is unavailable, say so and do not claim "
                    "access. Slack and Notion tools are read-only. "
                    "Distinguish the sources in your answer. Never invent Google events "
                    "or files, and do not claim Google access when unavailable. "
                    "Calendar and Drive tools are read-only; do not modify Google data. "
                    "Before every create_task call, use get_projects to resolve the "
                    "project name to its numeric ID. Never guess a project ID. "
                    "If the user did not name a project and get_projects returns "
                    "exactly one project, use that project. If it returns multiple "
                    "projects and the intended one is unclear, ask which project "
                    "the user means instead of creating a task. "
                    "When the user requests task creation and the required details "
                    "are known, call create_task after the lookups in this request. "
                    "Do not stop with a promise to create it later. A task is created "
                    "only when create_task returns a successful result. "
                    "Do not mention numeric IDs in the final answer unless the "
                    "user asks for them; task.id and task.project_id are different. "
                    f"Unavailable sources: {' '.join(unavailable) if unavailable else 'none'}. "
                    "After tool results, answer in natural language."
                ),
            },
            *messages,
        ]
        resolved_projects: dict[int, str] | None = None
        drive_results: list[tuple[str, dict]] = []
        other_results: list[tuple[str, str, dict]] = []
        drive_failed = False
        calendar_day = _calendar_day_request(latest_question)
        calendar_day_result: dict | None = None

        if slack_context and "search_messages" in tool_names and tool_sources.get("search_messages") == "Slack":
            if slack_context.clarification:
                choices = ", ".join(slack_context.clarification)
                return f"I found multiple matching projects: {choices}. Which project do you mean?"
            if not slack_context.channels:
                return f"No Slack channel is configured for {slack_context.label}. Which channel should I use?"
            total_scoped_messages = 0
            for channel in slack_context.channels:
                arguments = {"query": slack_search_query(channel, slack_context), "limit": 100}
                result = await tool_clients["search_messages"].call_tool("search_messages", arguments)
                data = result.structured_content
                if data is None and result.content and hasattr(result.content[0], "text"):
                    data = json.loads(result.content[0].text)
                if result.is_error or not isinstance(data, dict):
                    return f"Slack search failed for #{channel}; I could not verify messages for {slack_context.period}."
                scoped = [item for item in data.get("messages", []) if in_period(item, slack_context)]
                total_scoped_messages += len(scoped)
                conversation.append({"role": "assistant", "content": "", "tool_calls": [
                    {"type": "function", "function": {"name": "search_messages", "arguments": arguments}}]})
                conversation.append({"role": "tool", "tool_name": "search_messages", "content": json.dumps({
                    "source": "Slack", "context": slack_context.label, "channel": channel,
                    "period": slack_context.period, "start": slack_context.start.isoformat(),
                    "end_exclusive": slack_context.end.isoformat(), "messages": scoped,
                    "has_more": bool(data.get("has_more")),
                })})
            if total_scoped_messages == 0:
                return f"I found no accessible Slack messages in the configured {slack_context.label} channel(s) for {slack_context.period}."

        try:
            async with httpx.AsyncClient(timeout=120.0) as client:
                for _ in range(8):
                    response = await client.post(
                        f"{url}/api/chat",
                        json={"model": model, "messages": conversation, "tools": ollama_tools, "stream": False},
                    )
                    response.raise_for_status()
                    answer = response.json()["message"]
                    calls = answer.get("tool_calls") or []
                    if not calls:
                        reply = (answer.get("content") or "").strip()
                        if calendar_day and "get_events_by_date" in tool_names and tool_sources.get("get_events_by_date") == "Google Calendar":
                            if calendar_day_result is None:
                                result = await tool_clients["get_events_by_date"].call_tool("get_events_by_date", {"day": calendar_day[1]})
                                if result.is_error or not isinstance(result.structured_content, dict):
                                    return "Google Calendar query failed; I could not verify meetings for that date."
                                calendar_day_result = result.structured_content
                            return _render_calendar_day(calendar_day[0], calendar_day[1], calendar_day_result)
                        if drive_results:
                            return render_drive_results(drive_results, other_results)
                        if drive_failed:
                            return "Google Drive search failed; I could not verify any files for this request."
                        if drive_available and _drive_question(latest_question):
                            arguments = {"query": latest_question}
                            requested_type = _requested_file_type(latest_question)
                            if requested_type:
                                arguments["file_type"] = requested_type
                            result = await tool_clients["search_files"].call_tool("search_files", arguments)
                            if result.is_error or not isinstance(result.structured_content, dict):
                                return "Google Drive search failed; I could not verify any files for this request."
                            return render_drive_results([("search_files", result.structured_content)], other_results)
                        if _drive_question(latest_question) and not drive_available:
                            return "Google Drive integration is unavailable; I cannot verify any files for this request."
                        if not reply:
                            raise LLMError("Ollama returned an empty response. Please try again.")
                        return reply

                    conversation.append(answer)
                    for call in calls:
                        function = call["function"]
                        name = function["name"]
                        arguments = function.get("arguments") or {}
                        if isinstance(arguments, str):
                            arguments = json.loads(arguments)
                        if name == "search_files" and tool_sources.get(name) == "Google Drive":
                            arguments.pop("limit", None)
                            requested_type = _requested_file_type(latest_question)
                            if requested_type:
                                arguments["file_type"] = requested_type
                            else:
                                arguments.pop("file_type", None)
                        if name == "get_events_by_date" and calendar_day and tool_sources.get(name) == "Google Calendar":
                            arguments["day"] = calendar_day[1]
                        if name == "create_task" and resolved_projects is None:
                            result_text = json.dumps({"error": "Call get_projects to resolve the project ID before create_task. No task was created."})
                        elif name == "create_task" and arguments.get("project_id") not in resolved_projects:
                            result_text = json.dumps({"error": "That project ID was not returned by get_projects. No task was created."})
                        elif name not in tool_names:
                            result_text = json.dumps({"error": f"Unknown tool: {name}"})
                        else:
                            result = await tool_clients[name].call_tool(name, arguments)
                            if result.is_error:
                                if tool_sources[name] == "Google Drive":
                                    drive_failed = True
                                result_text = json.dumps({"error": "Tool failed", "details": [block.text for block in result.content if hasattr(block, "text")]})
                            else:
                                if name == "get_projects" and result.structured_content:
                                    resolved_projects = {project["id"]: project["name"] for project in result.structured_content["projects"]}
                                data = result.structured_content or [block.text for block in result.content if hasattr(block, "text")]
                                if name == "get_events_by_date" and calendar_day and isinstance(data, dict):
                                    calendar_day_result = data
                                if tool_sources[name] == "Google Drive" and isinstance(data, dict):
                                    drive_results.append((name, data))
                                elif tool_sources[name] != "Google Drive" and isinstance(data, dict):
                                    other_results.append((tool_sources[name], name, data))
                                result_text = json.dumps({**data, "source": tool_sources[name]} if isinstance(data, dict) else {"data": data, "source": tool_sources[name]})
                        conversation.append({"role": "tool", "tool_name": name, "content": result_text})
        except httpx.ConnectError as exc:
            raise LLMError("Could not connect to Ollama. Start Ollama and try again.") from exc
        except httpx.TimeoutException as exc:
            raise LLMError("Ollama timed out. Please try again.") from exc
        except httpx.RequestError as exc:
            raise LLMError("Could not reach Ollama. Check OLLAMA_BASE_URL and try again.") from exc
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 404:
                raise LLMError(f"Ollama model '{model}' was not found. Run: ollama pull {model}") from exc
            raise LLMError("Ollama could not complete the request. Please try again.") from exc
        except (ValueError, KeyError, TypeError) as exc:
            raise LLMError("Ollama returned an invalid response. Please try again.") from exc

        raise LLMError("The model called too many tools. Please try a simpler request.")


def get_llm() -> LLM:
    """Select the provider here when another implementation is added later."""
    return OllamaLLM()
