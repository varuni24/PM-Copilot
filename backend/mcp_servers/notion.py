"""Read-only Notion tools for pages shared with the configured connection."""

import os
from uuid import UUID

import httpx
from dotenv import load_dotenv
from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import BaseModel, Field

load_dotenv()

mcp = MCPServer("Notion", instructions="Search and read pages/data sources shared with this Notion connection. Never change Notion content.")
NOTION_API_URL = "https://api.notion.com/v1"
NOTION_VERSION = "2026-03-11"


class NotionPage(BaseModel):
    id: str
    title: str
    url: str | None
    last_edited_time: str | None


class PagesResult(BaseModel):
    pages: list[NotionPage]
    has_more: bool


class NotionDataSource(BaseModel):
    id: str
    title: str
    database_id: str | None
    url: str | None


class DatabasesResult(BaseModel):
    data_sources: list[NotionDataSource]
    has_more: bool


class NotionBlock(BaseModel):
    id: str
    type: str
    text: str
    has_children: bool


class PageResult(BaseModel):
    page: NotionPage
    properties: dict[str, str]
    blocks: list[NotionBlock]
    has_more: bool


def _request(method: str, path: str, *, params: dict | None = None, body: dict | None = None) -> dict:
    token = os.getenv("NOTION_API_TOKEN")
    if not token:
        raise ToolError("Notion is not authenticated. Set NOTION_API_TOKEN in backend/.env.")
    headers = {"Authorization": f"Bearer {token}", "Notion-Version": NOTION_VERSION, "Content-Type": "application/json"}
    try:
        with httpx.Client(timeout=20.0) as client:
            response = client.request(method, f"{NOTION_API_URL}{path}", params=params, json=body, headers=headers)
            response.raise_for_status()
            return response.json()
    except httpx.HTTPStatusError as exc:
        code = None
        try:
            code = exc.response.json().get("code")
        except ValueError:
            pass
        raise ToolError(f"Notion returned HTTP {exc.response.status_code}" + (f" ({code})" if code else "") + "; check token, read capability, and page access.") from exc
    except (httpx.RequestError, ValueError) as exc:
        raise ToolError("Notion API could not be reached or returned invalid JSON.") from exc


def _plain(rich_text: list[dict]) -> str:
    return "".join(item.get("plain_text", item.get("text", {}).get("content", "")) for item in rich_text)


def _page(data: dict) -> NotionPage:
    title_property = next((item for item in data.get("properties", {}).values() if item.get("type") == "title"), None)
    title = _plain(title_property.get("title", [])) if title_property else ""
    return NotionPage(id=data["id"], title=title, url=data.get("url"), last_edited_time=data.get("last_edited_time"))


def _search(query: str, object_type: str, limit: int) -> tuple[list[dict], bool]:
    results = []
    cursor = None
    while len(results) < limit:
        body = {"query": query.strip(), "page_size": min(100, limit - len(results)), "filter": {"property": "object", "value": object_type},
                "sort": {"direction": "descending", "timestamp": "last_edited_time"}}
        if cursor:
            body["start_cursor"] = cursor
        data = _request("POST", "/search", body=body)
        results.extend(data.get("results", []))
        cursor = data.get("next_cursor") if data.get("has_more") else None
        if not cursor:
            break
    return results, bool(cursor)


@mcp.tool()
def search_pages(query: str = "", limit: int = Field(default=20, ge=1, le=100)) -> PagesResult:
    """Search Notion page TITLES shared with this connection; query is a title term such as DS701, tracker, or risks. Omit query to list recently edited accessible pages. This is not a full-text content search; use get_page with a returned page ID to read its properties and top-level blocks. Returns real IDs, titles, URLs, and edit times."""
    results, has_more = _search(query, "page", limit)
    return PagesResult(pages=[_page(item) for item in results], has_more=has_more)


@mcp.tool()
def search_databases(query: str = "", limit: int = Field(default=20, ge=1, le=100)) -> DatabasesResult:
    """Find Notion database DATA SOURCES by title among databases shared with this connection. Notion's current API searches data sources rather than old database objects; returns their IDs and parent database IDs. Omit query to list recently edited accessible data sources. This does not read rows."""
    results, has_more = _search(query, "data_source", limit)
    sources = [NotionDataSource(id=item["id"], title=_plain(item.get("title", [])),
                                database_id=item.get("parent", {}).get("database_id"), url=item.get("url")) for item in results]
    return DatabasesResult(data_sources=sources, has_more=has_more)


def _property_text(item: dict) -> str:
    kind = item.get("type", "")
    value = item.get(kind)
    if kind in ("title", "rich_text"):
        return _plain(value or [])
    if kind in ("select", "status"):
        return (value or {}).get("name", "")
    if kind == "multi_select":
        return ", ".join(option.get("name", "") for option in value or [])
    if kind == "date":
        return (value or {}).get("start", "")
    if value is None:
        return ""
    if isinstance(value, (str, int, float, bool)):
        return str(value)
    return ""


@mcp.tool()
def get_page(page_id: str) -> PageResult:
    """Read one Notion page by an ID returned from search_pages. Returns its title, URL, property values, and up to 100 top-level content blocks. Blocks marked has_children have nested content not included here; do not claim the full nested page was read. Never create or edit Notion content."""
    try:
        safe_id = str(UUID(page_id))
    except ValueError as exc:
        raise ToolError("page_id must be a Notion UUID from search_pages.") from exc
    page_data = _request("GET", f"/pages/{safe_id}")
    properties = {name: _property_text(item) for name, item in page_data.get("properties", {}).items()}
    block_data = _request("GET", f"/blocks/{safe_id}/children", params={"page_size": 100})
    blocks = []
    for item in block_data.get("results", []):
        kind = item.get("type", "")
        content = item.get(kind, {})
        text = _plain(content.get("rich_text", [])) if isinstance(content, dict) else ""
        if kind == "child_page":
            text = content.get("title", "")
        blocks.append(NotionBlock(id=item["id"], type=kind, text=text, has_children=bool(item.get("has_children", False))))
    return PageResult(page=_page(page_data), properties=properties, blocks=blocks, has_more=bool(block_data.get("has_more")))


if __name__ == "__main__":
    mcp.run("streamable-http", host="127.0.0.1", port=8005)
