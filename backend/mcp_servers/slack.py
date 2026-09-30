"""Read-only Slack tools for conversations accessible to the authorized user token."""

import os
import re

import httpx
from dotenv import load_dotenv
from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import BaseModel, Field

load_dotenv()

mcp = MCPServer("Slack", instructions="Read authorized Slack messages and channels. Never send or edit messages.")
SLACK_API_URL = "https://slack.com/api"
_user_cache: dict[str, str] = {}


class SlackChannel(BaseModel):
    id: str
    name: str
    is_private: bool
    kind: str


class ChannelsResult(BaseModel):
    channels: list[SlackChannel]
    has_more: bool


class SlackMessage(BaseModel):
    text: str
    timestamp: str
    user_id: str | None
    user_name: str | None
    channel_id: str | None
    channel_name: str | None
    permalink: str | None


class MessagesResult(BaseModel):
    messages: list[SlackMessage]
    has_more: bool


def _api(method: str, params: dict | None = None, *, http_method: str = "GET") -> dict:
    token = os.getenv("SLACK_USER_TOKEN")
    if not token:
        raise ToolError("Slack is not authenticated. Set SLACK_USER_TOKEN in backend/.env.")
    try:
        with httpx.Client(timeout=20.0) as client:
            response = client.request(http_method, f"{SLACK_API_URL}/{method}", params=params, headers={"Authorization": f"Bearer {token}"})
            response.raise_for_status()
            data = response.json()
    except httpx.HTTPStatusError as exc:
        raise ToolError(f"Slack returned HTTP {exc.response.status_code}; check app permissions and rate limits.") from exc
    except (httpx.RequestError, ValueError) as exc:
        raise ToolError("Slack API could not be reached or returned invalid JSON.") from exc
    if not data.get("ok"):
        raise ToolError(f"Slack API error: {data.get('error', 'unknown_error')}. Check the user token and granted scopes.")
    return data


def _user_name(user_id: str | None) -> str | None:
    if user_id is None:
        return None
    if user_id in _user_cache:
        return _user_cache[user_id]

    try:
        data = _api("users.info", {"user": user_id})
        user = data.get("user") or {}
        profile = user.get("profile") or {}
        name = (
            profile.get("display_name")
            or profile.get("real_name")
            or user.get("name")
            or user_id
        )
    except Exception:
        name = user_id

    _user_cache[user_id] = name
    return name


def _render_mentions(text: str) -> str:
    def render_user(match: re.Match[str]) -> str:
        user_id, inline_name = match.group(1), match.group(2)
        return f"@{inline_name or _user_name(user_id) or user_id}"

    text = re.sub(r"<@(U[A-Z0-9]+)(?:\|([^>]+))?>", render_user, text)
    text = re.sub(r"<!(channel|here|everyone)>", r"@\1", text)
    return re.sub(r"<#C[A-Z0-9]+\|([^>]+)>", r"#\1", text)


def _channel(data: dict) -> SlackChannel:
    kind = "dm" if data.get("is_im") else "group_dm" if data.get("is_mpim") else "channel"
    return SlackChannel(id=data["id"], name=data.get("name") or data.get("user") or data["id"],
                        is_private=bool(data.get("is_private", kind != "channel")), kind=kind)


def _list_eligible(types: str) -> list[SlackChannel]:
    """Fail closed for channels/MPIMs without membership; listed IMs belong to this user token."""
    channels: list[SlackChannel] = []
    cursor = None
    while True:
        params = {"types": types, "exclude_archived": "true", "limit": 999}
        if cursor:
            params["cursor"] = cursor
        data = _api("conversations.list", params)
        for item in data.get("channels", []):
            if item.get("is_archived"):
                continue
            if item.get("is_im"):
                # Slack omits is_member on 1:1 DMs returned for the authenticated user.
                if item.get("is_member") is False:
                    continue
            elif item.get("is_member") is not True:
                continue
            channels.append(_channel(item))
        cursor = data.get("response_metadata", {}).get("next_cursor") or None
        if not cursor:
            break
    return channels


def _eligible_conversations() -> list[SlackChannel]:
    channels = _list_eligible("public_channel,private_channel")
    if os.getenv("SLACK_INCLUDE_DMS", "").lower() in ("1", "true", "yes"):
        for kind in ("im", "mpim"):
            try:
                channels.extend(_list_eligible(kind))
            except ToolError as exc:
                if "missing_scope" not in str(exc):
                    raise
                # DM scopes are optional; never fail the channel tools for their absence.
    return channels


@mcp.tool()
def get_channels(limit: int = Field(default=200, ge=1, le=500)) -> ChannelsResult:
    """List non-archived Slack channels the authenticated user joined. Optional DMs/group DMs appear only when SLACK_INCLUDE_DMS is enabled and their read scopes are granted. Returns IDs, names, and kinds; use an ID or exact name for history. has_more indicates additional eligible conversations."""
    channels = _eligible_conversations()
    return ChannelsResult(channels=channels[:limit], has_more=len(channels) > limit)


@mcp.tool()
def search_messages(query: str, limit: int = Field(default=20, ge=1, le=100)) -> MessagesResult:
    """Search messages only in non-archived joined channels. Optional DMs/group DMs are searched only when SLACK_INCLUDE_DMS is enabled and their read scopes permit listing. Pass a keyword or Slack search expression such as DS701 or 'deadline in:ds701'. Returns exact text, timestamps, and Slack-provided links; never infer inaccessible messages."""
    if not query.strip():
        raise ToolError("Slack search query cannot be empty.")
    allowed = {channel.id: channel for channel in _eligible_conversations()}
    messages: list[SlackMessage] = []
    has_more = False
    for page in range(1, 101):  # Slack search permits at most 100 pages.
        data = _api("search.messages", {"query": query.strip(), "count": 100, "page": page,
                                        "sort": "timestamp", "sort_dir": "desc"})
        matches = data.get("messages", {})
        page_matches = matches.get("matches", [])
        more_in_page = False
        for index, item in enumerate(page_matches):
            conversation = allowed.get(item.get("channel", {}).get("id"))
            if conversation is None:
                continue
            messages.append(SlackMessage(text=_render_mentions(item.get("text", "")), timestamp=item["ts"], user_id=item.get("user"),
                                         user_name=_user_name(item.get("user")),
                                         channel_id=conversation.id, channel_name=conversation.name,
                                         permalink=item.get("permalink")))
            if len(messages) == limit:
                more_in_page = any(rest.get("channel", {}).get("id") in allowed for rest in page_matches[index + 1:])
                break
        pagination = matches.get("pagination", {})
        page_count = pagination.get("page_count", matches.get("paging", {}).get("pages"))
        has_more = more_in_page or (page < page_count if page_count is not None else len(page_matches) == 100)
        if len(messages) == limit or not has_more:
            break
    return MessagesResult(messages=messages, has_more=has_more)


@mcp.tool()
def get_channel_history(channel: str, limit: int = Field(default=20, ge=1, le=100)) -> MessagesResult:
    """Read latest messages only in a non-archived joined channel or the user's listed DM/group DM. Pass an ID from get_channels or an exact channel name (optional #). DM IDs begin with D; group DM IDs begin with G. Requires the corresponding history scope. Never reads unjoined or archived conversations."""
    channel_id = channel.strip().lstrip("#")
    if not channel_id:
        raise ToolError("channel must be a Slack channel ID or name.")
    eligible = _eligible_conversations()
    match = next((item for item in eligible if item.id == channel_id or item.name.casefold() == channel_id.casefold()), None)
    if not match:
        raise ToolError(f"Slack conversation '{channel}' is not a non-archived conversation joined by this user.")
    channel_id, channel_name = match.id, match.name
    messages = []
    cursor = None
    while len(messages) < limit:
        params = {"channel": channel_id, "limit": limit - len(messages)}
        if cursor:
            params["cursor"] = cursor
        data = _api("conversations.history", params)
        messages.extend(
            SlackMessage(text=_render_mentions(item.get("text", "")), timestamp=item["ts"], user_id=item.get("user"),
                         user_name=_user_name(item.get("user")),
                         channel_id=channel_id, channel_name=channel_name, permalink=None)
            for item in data.get("messages", [])
        )
        cursor = data.get("response_metadata", {}).get("next_cursor") or None
        if not data.get("has_more") or not cursor:
            break
    return MessagesResult(messages=messages, has_more=bool(cursor))


if __name__ == "__main__":
    mcp.run("streamable-http", host="127.0.0.1", port=8004)
