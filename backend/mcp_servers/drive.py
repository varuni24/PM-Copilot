from __future__ import annotations

import re
from typing import Literal

from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import BaseModel, Field

from integrations.google_oauth import load_credentials

mcp = MCPServer("Google Drive", instructions="Search and read the authenticated user's Google Drive files. This server cannot modify files.")


class DriveFile(BaseModel):
    id: str
    name: str
    mime_type: str | None
    description: str | None
    modified_time: str | None
    link: str | None


class FilesResult(BaseModel):
    files: list[DriveFile]
    has_more: bool = False
    incomplete_search: bool = False


class FileResult(BaseModel):
    file: DriveFile
    content: str | None
    content_status: str


def _service():
    try:
        return build("drive", "v3", credentials=load_credentials("drive"), cache_discovery=False)
    except RuntimeError as exc:
        raise ToolError(str(exc)) from exc


def _file(data: dict) -> DriveFile:
    return DriveFile(
        id=data["id"],
        name=data["name"],
        mime_type=data.get("mimeType"),
        description=data.get("description"),
        modified_time=data.get("modifiedTime"),
        link=data.get("webViewLink"),
    )


def _escape_query(value: str) -> str:
    return value.replace("\\", "\\\\").replace("'", "\\'")


FILE_MIME_TYPES = {
    "presentation": (
        "application/vnd.google-apps.presentation",
        "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        "application/vnd.ms-powerpoint",
    ),
    "pdf": ("application/pdf",),
    "document": (
        "application/vnd.google-apps.document",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "application/msword",
    ),
    "folder": ("application/vnd.google-apps.folder",),
}

SEARCH_STOP_WORDS = {
    "a", "able", "about", "access", "all", "also", "an", "and", "any", "are",
    "bring", "can", "could", "describe", "did", "do", "does", "each", "every", "find",
    "for", "from", "get", "give", "had", "has", "have", "here", "how", "i", "in",
    "is", "just", "locate", "look", "many", "may", "me", "might", "more", "most",
    "must", "my", "need", "of", "on", "other", "our", "please", "provide", "pull",
    "put", "shall", "should", "show", "some", "such", "take", "tell", "than", "that",
    "the", "their", "them", "then", "there", "these", "this", "those", "to", "us",
    "very", "want", "was", "well", "were", "what", "when", "where", "which", "who",
    "whom", "whose", "why", "will", "with", "would", "you",
}


def _search_intent(query: str | None, file_type: str | None) -> tuple[str, str | None]:
    term = query.strip() if query else ""
    if file_type == "document" and term and not re.search(r"\b(documents?|docs?|word)\b", term, re.IGNORECASE):
        file_type = None
    if re.search(r"\b(ppts?|powerpoints?|presentations?|slides?)\b", term, re.IGNORECASE):
        file_type = file_type or "presentation"
        term = re.sub(r"\b(ppts?|powerpoints?|presentations?|slides?)\b", "", term, flags=re.IGNORECASE)
    elif re.search(r"\bpdfs?\b", term, re.IGNORECASE):
        file_type = file_type or "pdf"
        term = re.sub(r"\bpdfs?\b", "", term, flags=re.IGNORECASE)
    term = re.sub(r"^(?:what(?: all)?|is there any|are there any|do i have any|find|search(?: for)?|list|show(?: me)?)\b", "", term, flags=re.IGNORECASE)
    term = re.sub(r"\b(?:my|i have|files?|documents?|(?:in|from) google drive|(?:in|from) drive|related to|about)\b", " ", term, flags=re.IGNORECASE)
    return " ".join(term.split()).strip(" ?."), file_type


def _search_terms(term: str) -> list[str]:
    phrases = re.findall(r'"([^"\n]+)"', term)
    unquoted = re.sub(r'"[^"\n]+"', " ", term)
    keywords = [word for word in re.findall(r"[A-Za-z0-9_-]+", unquoted)
                if word.casefold() not in SEARCH_STOP_WORDS]
    return list(dict.fromkeys([phrase.strip() for phrase in phrases if phrase.strip()] + keywords))


def _keyword_clauses(term: str) -> list[str]:
    phrases = {phrase.strip() for phrase in re.findall(r'"([^"\n]+)"', term)}
    values = [f'"{value}"' if value in phrases else value for value in _search_terms(term)]
    return [f"fullText contains '{_escape_query(value)}'" for value in dict.fromkeys(values)]


@mcp.tool()
def search_files(query: str | None = None, file_type: Literal["presentation", "pdf", "document", "folder"] | None = None, limit: int = Field(default=20, ge=1, le=200)) -> FilesResult:
    """Search accessible My Drive, shared-with-me, and Shared drives for files, folders, resumes, project-management files, PDFs, or presentations. Pass a keyword or natural request as query. Set file_type only when the user explicitly requests a specific type (PPT/PowerPoint, PDF, document, or folder); leave it unset for generic files, resumes, or project-management files so all MIME types can match. Omit both to list recent files. Course codes match DS 701/DS701 and DS594/DS 594 variants. Pages through matches up to limit and reports has_more if further matches exist. Use this whenever the user asks what files they have, even without saying 'Google Drive'."""
    term, file_type = _search_intent(query, file_type)
    ranking_terms = _search_terms(term)
    drive_query = "trashed = false"
    name_matches = None
    has_full_text = False
    if term:
        course_code = re.fullmatch(r"([A-Za-z]+)[\s_-]*([0-9]+)", term)
        if course_code:
            letters, number = course_code.groups()
            name_matches = re.compile(rf"(?<![A-Za-z0-9]){re.escape(letters)}[\s_-]*{re.escape(number)}(?![0-9])", re.IGNORECASE)
            variants = {f"{prefix}{separator}{number}" for prefix in (letters.upper(), letters.lower()) for separator in ("", " ", "-", "_")}
            clauses = []
            for value in sorted(variants):
                search_term = f'"{value}"' if " " in value else value
                clauses.append(f"fullText contains '{_escape_query(search_term)}'")
            joiner = " or "
        else:
            clauses = _keyword_clauses(term)
            joiner = " or "
        if clauses:
            drive_query += " and (" + joiner.join(clauses) + ")"
            has_full_text = True
    if file_type:
        mime_clauses = [f"mimeType = '{mime_type}'" for mime_type in FILE_MIME_TYPES[file_type]]
        drive_query += " and (" + " or ".join(mime_clauses) + ")"
    try:
        files = []
        page_token = None
        incomplete_search = False
        service = _service()
        while len(files) < limit:
            request_options = {
                "q": drive_query,
                "corpora": "allDrives",
                "includeItemsFromAllDrives": True,
                "supportsAllDrives": True,
                "pageSize": limit - len(files),
                "pageToken": page_token,
                "fields": "nextPageToken,incompleteSearch,files(id,name,mimeType,description,modifiedTime,webViewLink)",
            }
            order_by = "relevance desc" if has_full_text else "modifiedTime desc"
            try:
                response = service.files().list(**request_options, orderBy=order_by).execute()
            except HttpError as exc:
                if not has_full_text or exc.resp.status != 400:
                    raise
                response = service.files().list(**request_options, orderBy="modifiedTime desc").execute()
            incomplete_search = incomplete_search or response.get("incompleteSearch", False)
            files.extend(data for data in response.get("files", []) if name_matches is None or name_matches.search(data.get("name", "")))
            page_token = response.get("nextPageToken")
            if not page_token:
                break
    except HttpError as exc:
        raise ToolError(f"Google Drive API returned HTTP {exc.resp.status}. Check API access and OAuth scopes.") from exc
    files.sort(
        key=lambda data: (
            sum(keyword.casefold() in data.get("name", "").casefold() for keyword in ranking_terms),
            data.get("modifiedTime") or "",
        ),
        reverse=True,
    )
    return FilesResult(files=[_file(data) for data in files], has_more=bool(page_token), incomplete_search=incomplete_search)


@mcp.tool()
def get_file(file_id: str) -> FileResult:
    """Get a Google Drive file by ID from search_files. Returns metadata and text content for Google Docs or small plain-text files; other file types return metadata with a content_status explaining the limit. Never edit the file."""
    service = _service()
    try:
        data = service.files().get(
            fileId=file_id,
            fields="id,name,mimeType,description,modifiedTime,webViewLink,size,capabilities(canDownload)",
        ).execute()
        file = _file(data)
        mime_type = data.get("mimeType", "")
        if not data.get("capabilities", {}).get("canDownload", True):
            return FileResult(file=file, content=None, content_status="Download is not allowed for this file.")
        if mime_type == "application/vnd.google-apps.document":
            content = service.files().export(fileId=file_id, mimeType="text/plain").execute()
        elif mime_type.startswith("text/") and int(data.get("size", 0)) <= 200_000:
            content = service.files().get_media(fileId=file_id).execute()
        else:
            return FileResult(file=file, content=None, content_status="Text content is not available for this file type or size.")
    except HttpError as exc:
        raise ToolError(f"Google Drive API returned HTTP {exc.resp.status}. Check file access, API access, and OAuth scopes.") from exc
    text = content.decode("utf-8", errors="replace") if isinstance(content, bytes) else str(content)
    return FileResult(file=file, content=text[:20_000], content_status="Text content retrieved; truncated to 20,000 characters if longer.")


if __name__ == "__main__":
    mcp.run("streamable-http", host="127.0.0.1", port=8003)
