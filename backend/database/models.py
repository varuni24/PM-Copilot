import os
from datetime import date, datetime, timezone

from dotenv import load_dotenv
from sqlalchemy import Date, DateTime, ForeignKey, String, Text, create_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship, sessionmaker

load_dotenv()

DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql+psycopg://pm_copilot:pm_copilot@localhost:5432/pm_copilot",
)
engine = create_engine(DATABASE_URL)
SessionLocal = sessionmaker(bind=engine)


class Base(DeclarativeBase):
    pass


class Project(Base):
    __tablename__ = "projects"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200), unique=True)
    description: Mapped[str | None] = mapped_column(Text)
    tasks: Mapped[list["Task"]] = relationship(back_populates="project")
    decisions: Mapped[list["Decision"]] = relationship(back_populates="project")
    risks: Mapped[list["Risk"]] = relationship(back_populates="project")


class Person(Base):
    __tablename__ = "people"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200), unique=True)
    email: Mapped[str | None] = mapped_column(String(255))
    tasks: Mapped[list["Task"]] = relationship(back_populates="owner")


class Task(Base):
    __tablename__ = "tasks"

    id: Mapped[int] = mapped_column(primary_key=True)
    title: Mapped[str] = mapped_column(String(200))
    description: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(30), default="todo")
    owner_id: Mapped[int | None] = mapped_column(ForeignKey("people.id"))
    owner: Mapped[Person | None] = relationship(back_populates="tasks")
    due_date: Mapped[date | None] = mapped_column(Date)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"))
    project: Mapped[Project] = relationship(back_populates="tasks")


class Decision(Base):
    __tablename__ = "decisions"

    id: Mapped[int] = mapped_column(primary_key=True)
    title: Mapped[str] = mapped_column(String(200))
    description: Mapped[str | None] = mapped_column(Text)
    decided_on: Mapped[date | None] = mapped_column(Date)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"))
    project: Mapped[Project] = relationship(back_populates="decisions")


class Risk(Base):
    __tablename__ = "risks"

    id: Mapped[int] = mapped_column(primary_key=True)
    title: Mapped[str] = mapped_column(String(200))
    description: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(30), default="open")
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"))
    project: Mapped[Project] = relationship(back_populates="risks")


class Conversation(Base):
    __tablename__ = "conversations"

    id: Mapped[int] = mapped_column(primary_key=True)
    title: Mapped[str] = mapped_column(String(120), default="New conversation")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))
    messages: Mapped[list["Message"]] = relationship(back_populates="conversation", cascade="all, delete-orphan", order_by="Message.created_at")


class Message(Base):
    __tablename__ = "messages"

    id: Mapped[int] = mapped_column(primary_key=True)
    conversation_id: Mapped[int] = mapped_column(ForeignKey("conversations.id", ondelete="CASCADE"), index=True)
    role: Mapped[str] = mapped_column(String(20))
    content: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    conversation: Mapped[Conversation] = relationship(back_populates="messages")


def initialize_database() -> None:
    Base.metadata.create_all(engine)
