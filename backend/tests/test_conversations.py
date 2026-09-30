import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app import main
from database.models import Base


class FakeLLM:
    async def reply(self, messages):
        return f"Answer to: {messages[-1]['content']}"


class ConversationEndpointTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.engine = create_engine(
            "sqlite+pysqlite:///:memory:",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(cls.engine)
        cls.sessions = sessionmaker(bind=cls.engine)

    def setUp(self):
        Base.metadata.drop_all(self.engine)
        Base.metadata.create_all(self.engine)
        self.session_patch = patch.object(main, "SessionLocal", self.sessions)
        self.init_patch = patch.object(main, "initialize_database", lambda: None)
        self.llm_patch = patch.object(main, "get_llm", return_value=FakeLLM())
        self.session_patch.start()
        self.init_patch.start()
        self.llm_patch.start()
        self.client_context = TestClient(main.app)
        self.client = self.client_context.__enter__()

    def tearDown(self):
        self.client_context.__exit__(None, None, None)
        self.llm_patch.stop()
        self.init_patch.stop()
        self.session_patch.stop()

    def test_conversation_crud_and_chat_persistence(self):
        created = self.client.post("/conversations")
        self.assertEqual(created.status_code, 201)
        conversation_id = created.json()["id"]

        first = self.client.post("/chat", json={
            "conversation_id": conversation_id,
            "messages": [{"role": "user", "content": "What tasks are overdue for the launch?"}],
        })
        self.assertEqual(first.status_code, 200)
        self.assertEqual(first.json()["conversation_id"], conversation_id)

        detail = self.client.get(f"/conversations/{conversation_id}")
        self.assertEqual(detail.status_code, 200)
        self.assertEqual(detail.json()["title"], "What tasks are overdue for the launch?")
        self.assertEqual([message["role"] for message in detail.json()["messages"]], ["user", "assistant"])

        legacy = self.client.post("/chat", json={
            "messages": [{"role": "user", "content": "Create this without an ID"}],
        })
        self.assertEqual(legacy.status_code, 200)
        self.assertIsInstance(legacy.json()["conversation_id"], int)

        listed = self.client.get("/conversations")
        self.assertEqual(listed.status_code, 200)
        self.assertEqual(len(listed.json()), 2)
        self.assertEqual(listed.json()[0]["id"], legacy.json()["conversation_id"])

        deleted = self.client.delete(f"/conversations/{conversation_id}")
        self.assertEqual(deleted.status_code, 204)
        self.assertEqual(self.client.get(f"/conversations/{conversation_id}").status_code, 404)

    def test_rename_conversation_success_and_not_found(self):
        created = self.client.post("/conversations").json()
        renamed = self.client.patch(
            f"/conversations/{created['id']}",
            json={"title": "  Weekly project briefing  "},
        )
        self.assertEqual(renamed.status_code, 200)
        self.assertEqual(renamed.json()["title"], "Weekly project briefing")
        self.assertEqual(
            self.client.get(f"/conversations/{created['id']}").json()["title"],
            "Weekly project briefing",
        )

        missing = self.client.patch("/conversations/99999", json={"title": "Missing"})
        self.assertEqual(missing.status_code, 404)


if __name__ == "__main__":
    unittest.main()
