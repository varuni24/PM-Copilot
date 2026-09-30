import os
from datetime import datetime, timezone

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Response, status
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from database.models import Conversation, Message as StoredMessage, SessionLocal, initialize_database
from app.llm import LLMError, get_llm

load_dotenv()

app = FastAPI(title="PM Copilot API")
app.add_middleware(
    CORSMiddleware,
    allow_origins=[os.getenv("FRONTEND_ORIGIN", "http://localhost:3000")],
    allow_credentials=False,
    allow_methods=["GET", "POST", "PATCH", "DELETE"],
    allow_headers=["Content-Type"],
)


class Message(BaseModel):
    role: str = Field(pattern="^(user|assistant)$")
    content: str = Field(min_length=1, max_length=10000)


class ChatRequest(BaseModel):
    messages: list[Message] = Field(min_length=1, max_length=30)
    conversation_id: int | None = None


class ChatResponse(BaseModel):
    reply: str
    conversation_id: int


class ConversationSummary(BaseModel):
    id: int
    title: str
    created_at: datetime
    updated_at: datetime


class ConversationDetail(ConversationSummary):
    messages: list[Message]


class ConversationUpdate(BaseModel):
    title: str

    @field_validator("title")
    @classmethod
    def validate_title(cls, value: str) -> str:
        title = value.strip()
        if not 1 <= len(title) <= 120:
            raise ValueError("Title must be between 1 and 120 characters.")
        return title


def make_title(content: str) -> str:
    title = " ".join(content.split())
    return title if len(title) <= 60 else f"{title[:57].rstrip()}…"


@app.on_event("startup")
def create_database_tables() -> None:
    initialize_database()


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/conversations", response_model=ConversationSummary, status_code=status.HTTP_201_CREATED)
def create_conversation() -> ConversationSummary:
    with SessionLocal.begin() as session:
        conversation = Conversation()
        session.add(conversation)
        session.flush()
        return ConversationSummary(
            id=conversation.id,
            title=conversation.title,
            created_at=conversation.created_at,
            updated_at=conversation.updated_at,
        )


@app.get("/conversations", response_model=list[ConversationSummary])
def list_conversations() -> list[Conversation]:
    with SessionLocal() as session:
        return list(session.scalars(select(Conversation).order_by(Conversation.updated_at.desc(), Conversation.id.desc())))


@app.get("/conversations/{conversation_id}", response_model=ConversationDetail)
def get_conversation(conversation_id: int) -> Conversation:
    with SessionLocal() as session:
        conversation = session.scalar(
            select(Conversation).options(selectinload(Conversation.messages)).where(Conversation.id == conversation_id)
        )
        if conversation is None:
            raise HTTPException(status_code=404, detail="Conversation not found.")
        return conversation


@app.patch("/conversations/{conversation_id}", response_model=ConversationSummary)
def rename_conversation(conversation_id: int, update: ConversationUpdate) -> ConversationSummary:
    with SessionLocal.begin() as session:
        conversation = session.get(Conversation, conversation_id)
        if conversation is None:
            raise HTTPException(status_code=404, detail="Conversation not found.")
        conversation.title = update.title
        conversation.updated_at = datetime.now(timezone.utc)
        session.flush()
        return ConversationSummary(
            id=conversation.id,
            title=conversation.title,
            created_at=conversation.created_at,
            updated_at=conversation.updated_at,
        )


@app.delete("/conversations/{conversation_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_conversation(conversation_id: int) -> Response:
    with SessionLocal.begin() as session:
        conversation = session.get(Conversation, conversation_id)
        if conversation is None:
            raise HTTPException(status_code=404, detail="Conversation not found.")
        session.delete(conversation)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@app.post("/chat", response_model=ChatResponse)
async def chat(request: ChatRequest) -> ChatResponse:
    if request.messages[-1].role != "user":
        raise HTTPException(status_code=422, detail="The last message must be from the user.")

    user_content = request.messages[-1].content
    with SessionLocal.begin() as session:
        conversation = session.get(Conversation, request.conversation_id) if request.conversation_id else None
        if request.conversation_id and conversation is None:
            raise HTTPException(status_code=404, detail="Conversation not found.")
        if conversation is None:
            conversation = Conversation(title=make_title(user_content))
            session.add(conversation)
            session.flush()
        elif not conversation.messages:
            conversation.title = make_title(user_content)
        conversation.updated_at = datetime.now(timezone.utc)
        session.add(StoredMessage(conversation=conversation, role="user", content=user_content))
        session.flush()
        conversation_id = conversation.id

    try:
        reply = await get_llm().reply([message.model_dump() for message in request.messages])
    except LLMError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    with SessionLocal.begin() as session:
        conversation = session.get(Conversation, conversation_id)
        if conversation is not None:
            conversation.updated_at = datetime.now(timezone.utc)
            session.add(StoredMessage(conversation=conversation, role="assistant", content=reply))
    return ChatResponse(reply=reply, conversation_id=conversation_id)
