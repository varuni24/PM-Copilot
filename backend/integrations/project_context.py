"""Backend-only PM context resolution; Slack MCP tools remain generic."""

import json
import os
import re
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo


@dataclass(frozen=True)
class SlackContext:
    label: str
    channels: tuple[str, ...]
    start: datetime
    end: datetime
    period: str
    explicit: bool
    clarification: tuple[str, ...] = ()


def _config() -> dict:
    path = Path(os.getenv("PM_CONTEXT_FILE", Path(__file__).with_name("project_context.json")))
    if not path.is_absolute():
        path = Path(__file__).parent / path
    return json.loads(path.read_text())


def _period(question: str, now: datetime) -> tuple[datetime, datetime, str]:
    today = now.date()
    midnight = lambda day: datetime.combine(day, time.min, tzinfo=now.tzinfo)
    if re.search(r"\byesterday\b", question, re.I):
        return midnight(today - timedelta(days=1)), midnight(today), "yesterday"
    if re.search(r"\btoday\b", question, re.I):
        return midnight(today), midnight(today + timedelta(days=1)), "today"
    if re.search(r"\bthis week\b", question, re.I):
        monday = today - timedelta(days=today.weekday())
        return midnight(monday), midnight(monday + timedelta(days=7)), "this week (Monday–Sunday)"
    if re.search(r"\b(past week|past 7 days|last week|recently|recent)\b", question, re.I):
        return now - timedelta(days=7), now, "past 7 days"
    return now - timedelta(days=7), now, "past 7 days"


def is_slack_question(question: str) -> bool:
    return bool(re.search(r"\b(slack|channels?|messages?|discuss(?:ed|ion)?|said|mention(?:ed)?|action items?)\b", question, re.I))


_PROJECT_STOP_WORDS = {
    "a", "an", "and", "any", "for", "in", "of", "on", "project", "recent",
    "summarize", "the", "this", "today", "update", "updates", "week", "what",
}


def _project_matches(question: str, projects: dict) -> tuple[str, ...]:
    question_tokens = set(re.findall(r"[a-z0-9]+", question.casefold()))
    normalized_question = " ".join(re.findall(r"[a-z0-9]+", question.casefold()))
    exact = [name for name in projects if " ".join(re.findall(r"[a-z0-9]+", name.casefold())) in normalized_question]
    if exact:
        longest = max(len(name) for name in exact)
        return tuple(name for name in exact if len(name) == longest)

    scored: list[tuple[int, str]] = []
    for name in projects:
        tokens = {token for token in re.findall(r"[a-z0-9]+", name.casefold())
                  if token not in _PROJECT_STOP_WORDS and len(token) >= 3}
        score = len(tokens & question_tokens)
        if score:
            scored.append((score, name))
    if not scored:
        return ()
    best = max(score for score, _ in scored)
    return tuple(name for score, name in scored if score == best)


def resolve_slack_context(question: str, now: datetime | None = None) -> SlackContext | None:
    config = _config()
    now = now or datetime.now(ZoneInfo("America/New_York"))
    start, end, period = _period(question, now)
    channel = re.search(r"(?:#|\bin:#)([a-z0-9][a-z0-9_-]*)", question, re.I)
    if channel:
        name = channel.group(1)
        return SlackContext(name, (name,), start, end, period, True)
    course = re.search(r"\bDS\s*\d+\b", question, re.I)
    key = re.sub(r"\s+", "", course.group()).casefold() if course else config["active_course"]
    entry = config.get("courses", {}).get(key)
    if not entry:
        if not is_slack_question(question):
            return None
        return SlackContext(course.group().upper() if course else key, (), start, end, period, bool(course))
    projects = entry["slack"].get("projects", {})
    matches = _project_matches(question, projects)
    if len(matches) > 1:
        return SlackContext(entry["code"], (), start, end, period, True, matches)
    if matches:
        project_name = matches[0]
        project = projects[project_name]
        return SlackContext(f"{entry['code']} / {project_name}", tuple(project.get("channels", [])),
                            start, end, period, True)
    if not is_slack_question(question) and not course:
        return None
    return SlackContext(entry["code"], tuple(entry["slack"].get("general_channels", [])),
                        start, end, period, bool(course))


def slack_search_query(channel: str, context: SlackContext) -> str:
    # Slack after:/before: use dates; one extra boundary day is filtered precisely below.
    after = (context.start.date() - timedelta(days=1)).isoformat()
    before = (context.end.date() + timedelta(days=1)).isoformat()
    return f"in:#{channel} after:{after} before:{before}"


def in_period(message: dict, context: SlackContext) -> bool:
    try:
        timestamp = datetime.fromtimestamp(float(message["timestamp"]), ZoneInfo("America/New_York"))
    except (KeyError, TypeError, ValueError):
        return False
    return context.start <= timestamp < context.end
