import os
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import BaseModel, Field

from integrations.google_oauth import load_credentials

load_dotenv()

mcp = MCPServer("Google Calendar", instructions="Read events from the authenticated user's Google Calendar. This server cannot modify events.")


class CalendarEvent(BaseModel):
    id: str
    title: str
    start: str
    end: str | None
    location: str | None
    description: str | None
    link: str | None


class EventsResult(BaseModel):
    events: list[CalendarEvent]
    timezone: str


class WeekEventsResult(EventsResult):
    week_start: str
    week_end: str


def _timezone() -> ZoneInfo:
    return ZoneInfo(os.getenv("GOOGLE_TIMEZONE", "America/New_York"))


def _service():
    try:
        return build("calendar", "v3", credentials=load_credentials("calendar"), cache_discovery=False)
    except RuntimeError as exc:
        raise ToolError(str(exc)) from exc


def _events(response: dict) -> list[CalendarEvent]:
    return [
        CalendarEvent(
            id=event["id"],
            title=event.get("summary", "(Untitled event)"),
            start=event.get("start", {}).get("dateTime") or event.get("start", {}).get("date", ""),
            end=event.get("end", {}).get("dateTime") or event.get("end", {}).get("date"),
            location=event.get("location"),
            description=event.get("description"),
            link=event.get("htmlLink"),
        )
        for event in response.get("items", [])
        if event.get("status") != "cancelled"
    ]


@mcp.tool()
def get_upcoming_events(limit: int = Field(default=10, ge=1, le=25)) -> EventsResult:
    """Get the next upcoming Google Calendar events, starting now, sorted by start time. Use this for 'What is my next meeting?' or upcoming meetings. Returns titles, times, locations, and links in the configured timezone."""
    tz = _timezone()
    try:
        response = _service().events().list(
            calendarId=os.getenv("GOOGLE_CALENDAR_ID", "primary"),
            timeMin=datetime.now(tz).isoformat(),
            maxResults=limit,
            singleEvents=True,
            orderBy="startTime",
        ).execute()
    except HttpError as exc:
        raise ToolError(f"Google Calendar API returned HTTP {exc.resp.status}. Check API access and OAuth scopes.") from exc
    return EventsResult(events=_events(response), timezone=str(tz))


@mcp.tool()
def get_events_by_date(day: str) -> EventsResult:
    """Get Google Calendar events on one local date. Pass an ISO date (YYYY-MM-DD); use today's or tomorrow's date for those questions. Returns titles, start/end times, locations, and links."""
    try:
        selected = date.fromisoformat(day)
    except ValueError as exc:
        raise ToolError("day must be an ISO date in YYYY-MM-DD format.") from exc
    tz = _timezone()
    start = datetime.combine(selected, time.min, tzinfo=tz)
    end = datetime.combine(selected + timedelta(days=1), time.min, tzinfo=tz)
    try:
        response = _service().events().list(
            calendarId=os.getenv("GOOGLE_CALENDAR_ID", "primary"),
            timeMin=start.isoformat(),
            timeMax=end.isoformat(),
            singleEvents=True,
            orderBy="startTime",
            maxResults=50,
        ).execute()
    except HttpError as exc:
        raise ToolError(f"Google Calendar API returned HTTP {exc.resp.status}. Check API access and OAuth scopes.") from exc
    return EventsResult(events=_events(response), timezone=str(tz))


@mcp.tool()
def get_events_this_week() -> WeekEventsResult:
    """Get Google Calendar meetings/events in the current local calendar week, Monday through Sunday, in America/New_York. Use for 'this week'; never use unbounded upcoming events for that request. Returns week dates and events, excluding the following Monday and later."""
    tz = ZoneInfo("America/New_York")
    today = datetime.now(tz).date()
    monday = today - timedelta(days=today.weekday())
    next_monday = monday + timedelta(days=7)
    start = datetime.combine(monday, time.min, tzinfo=tz)
    end = datetime.combine(next_monday, time.min, tzinfo=tz)
    try:
        response = _service().events().list(
            calendarId=os.getenv("GOOGLE_CALENDAR_ID", "primary"),
            timeMin=start.isoformat(),
            timeMax=end.isoformat(),
            singleEvents=True,
            orderBy="startTime",
            maxResults=250,
        ).execute()
    except HttpError as exc:
        raise ToolError(f"Google Calendar API returned HTTP {exc.resp.status}. Check API access and OAuth scopes.") from exc
    events = []
    for event in _events(response):
        event_start = datetime.fromisoformat(event.start)
        local_day = event_start.astimezone(tz).date() if event_start.tzinfo else event_start.date()
        if monday <= local_day < next_monday:
            events.append(event)
    return WeekEventsResult(events=events, timezone=str(tz), week_start=monday.isoformat(), week_end=(next_monday - timedelta(days=1)).isoformat())


if __name__ == "__main__":
    mcp.run("streamable-http", host="127.0.0.1", port=8002)
