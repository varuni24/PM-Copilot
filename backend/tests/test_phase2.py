import json
import os
import unittest
from unittest.mock import patch

import httpx
from mcp import Client
from sqlalchemy import delete

from database.models import Decision, Person, Project, Risk, SessionLocal, Task, initialize_database
from database.seed import seed_database
from app.llm import OllamaLLM
from mcp_servers.postgres import mcp


class Phase2IntegrationTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls) -> None:
        if not os.getenv("DATABASE_URL", "").startswith("postgresql+"):
            raise RuntimeError("Set DATABASE_URL to a disposable PostgreSQL test database before running these tests.")
        initialize_database()
        with SessionLocal.begin() as session:
            for model in (Task, Decision, Risk, Person, Project):
                session.execute(delete(model))
        seed_database()

    async def test_mcp_tools_against_postgres(self) -> None:
        async with Client(mcp) as client:
            tools = (await client.list_tools()).tools
            self.assertEqual({tool.name for tool in tools}, {"get_projects", "get_tasks", "get_overdue_tasks", "get_project", "get_people", "create_task"})
            self.assertTrue(all(tool.input_schema and tool.output_schema for tool in tools))

            projects = await client.call_tool("get_projects", {})
            self.assertFalse(projects.is_error)
            seeded = next(project for project in projects.structured_content["projects"] if project["name"] == "PM Copilot Launch")
            project_id = seeded["id"]
            self.assertIsInstance(project_id, int)
            self.assertTrue(seeded["description"])

            people = await client.call_tool("get_people", {})
            self.assertFalse(people.is_error)
            self.assertIn("Sarah", [person["name"] for person in people.structured_content["people"]])

            tasks = await client.call_tool("get_tasks", {"owner_name": "Sarah"})
            self.assertFalse(tasks.is_error)
            self.assertTrue(all(task["owner"] == "Sarah" for task in tasks.structured_content["tasks"]))

            overdue = await client.call_tool("get_overdue_tasks", {})
            self.assertFalse(overdue.is_error)
            overdue_by_title = {task["title"]: task for task in overdue.structured_content["tasks"]}
            self.assertEqual(overdue_by_title["Finish API documentation"]["owner"], "Sarah")
            self.assertEqual(overdue_by_title["Review launch checklist"]["owner"], "Alex")

            project = await client.call_tool("get_project", {"project_id": project_id})
            self.assertFalse(project.is_error)
            self.assertEqual(project.structured_content["name"], "PM Copilot Launch")
            self.assertEqual(len(project.structured_content["decisions"]), 1)
            self.assertEqual(len(project.structured_content["risks"]), 1)

            created = await client.call_tool("create_task", {"title": "Integration test task", "project_id": project_id, "owner_name": "Sarah", "due_date": "2026-09-18"})
            self.assertFalse(created.is_error)
            self.assertEqual(created.structured_content["task"]["owner"], "Sarah")
            self.assertEqual(created.structured_content["task"]["status"], "todo")

            invalid = await client.call_tool("create_task", {"title": "Should not be created", "project_id": project_id, "owner_name": "Unknown Person"})
            self.assertTrue(invalid.is_error)
            self.assertIn("No task was created", invalid.content[0].text)

    async def test_llm_tool_round_trip_against_postgres(self) -> None:
        requests = []

        def handler(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content)
            requests.append(body)
            if len(requests) == 1:
                self.assertIn("get_overdue_tasks", [tool["function"]["name"] for tool in body["tools"]])
                return httpx.Response(200, json={"message": {"role": "assistant", "content": "", "tool_calls": [{"type": "function", "function": {"name": "get_overdue_tasks", "arguments": {}}}]}})
            tool_message = body["messages"][-1]
            self.assertEqual(tool_message["role"], "tool")
            data = json.loads(tool_message["content"])
            overdue_by_title = {task["title"]: task for task in data["tasks"]}
            self.assertEqual(overdue_by_title["Finish API documentation"]["owner"], "Sarah")
            self.assertEqual(overdue_by_title["Review launch checklist"]["owner"], "Alex")
            return httpx.Response(200, json={"message": {"role": "assistant", "content": "Sarah's API documentation and Alex's launch checklist are overdue."}})

        original_client = httpx.AsyncClient

        def mock_client(*args, **kwargs):
            kwargs["transport"] = httpx.MockTransport(handler)
            return original_client(*args, **kwargs)

        with patch("app.llm.Client", side_effect=lambda url: Client(mcp)), patch("app.llm.httpx.AsyncClient", side_effect=mock_client):
            reply = await OllamaLLM().reply([{"role": "user", "content": "What tasks are overdue?"}])

        self.assertEqual(reply, "Sarah's API documentation and Alex's launch checklist are overdue.")
        self.assertEqual(len(requests), 2)

    async def test_creation_resolves_project_before_write(self) -> None:
        requests = []
        project_id = None

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal project_id
            body = json.loads(request.content)
            requests.append(body)
            if len(requests) == 1:
                self.assertIn("get_projects", [tool["function"]["name"] for tool in body["tools"]])
                self.assertIn("Before every create_task call, use get_projects", body["messages"][0]["content"])
                return httpx.Response(200, json={"message": {"role": "assistant", "content": "", "tool_calls": [{"type": "function", "function": {"name": "get_projects", "arguments": {}}}]}})
            if len(requests) == 2:
                projects = json.loads(body["messages"][-1]["content"])["projects"]
                project_id = next(project["id"] for project in projects if project["name"] == "PM Copilot Launch")
                return httpx.Response(200, json={"message": {"role": "assistant", "content": "", "tool_calls": [{"type": "function", "function": {"name": "create_task", "arguments": {"title": "Prepare the stakeholder status update", "project_id": project_id, "owner_name": "Sarah", "due_date": "2026-09-19"}}}]}})
            created = json.loads(body["messages"][-1]["content"])["task"]
            self.assertEqual(created["project_id"], project_id)
            self.assertEqual(created["owner"], "Sarah")
            self.assertEqual(created["due_date"], "2026-09-19")
            return httpx.Response(200, json={"message": {"role": "assistant", "content": "I created Sarah's stakeholder status update task for September 19, 2026."}})

        original_client = httpx.AsyncClient

        def mock_client(*args, **kwargs):
            kwargs["transport"] = httpx.MockTransport(handler)
            return original_client(*args, **kwargs)

        question = "Create a task for Sarah to prepare the stakeholder status update by September 19, 2026"
        with patch("app.llm.Client", side_effect=lambda url: Client(mcp)), patch("app.llm.httpx.AsyncClient", side_effect=mock_client):
            reply = await OllamaLLM().reply([{"role": "user", "content": question}])

        self.assertIn("I created", reply)
        self.assertEqual(len(requests), 3)

    async def test_guessed_project_id_is_not_sent_to_create_task(self) -> None:
        requests = []
        tool_calls = []

        def handler(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content)
            requests.append(body)
            if len(requests) == 1:
                return httpx.Response(200, json={"message": {"role": "assistant", "content": "", "tool_calls": [{"type": "function", "function": {"name": "create_task", "arguments": {"title": "Should never be written", "project_id": 5, "owner_name": "Sarah"}}}]}})
            error = json.loads(body["messages"][-1]["content"])["error"]
            self.assertIn("Call get_projects", error)
            return httpx.Response(200, json={"message": {"role": "assistant", "content": "I need to look up the project first."}})

        original_http_client = httpx.AsyncClient
        original_call_tool = Client.call_tool

        def mock_http_client(*args, **kwargs):
            kwargs["transport"] = httpx.MockTransport(handler)
            return original_http_client(*args, **kwargs)

        async def traced(client, name, arguments=None, **kwargs):
            tool_calls.append(name)
            return await original_call_tool(client, name, arguments, **kwargs)

        with patch("app.llm.Client", side_effect=lambda url: Client(mcp)), patch("app.llm.httpx.AsyncClient", side_effect=mock_http_client), patch.object(Client, "call_tool", traced):
            await OllamaLLM().reply([{"role": "user", "content": "Create a task for Sarah"}])

        self.assertNotIn("create_task", tool_calls)


if __name__ == "__main__":
    unittest.main()
