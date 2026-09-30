"""Render Drive metadata from MCP results without letting model prose invent files."""

import json


def render_drive_results(results: list[tuple[str, dict]], other_results: list[tuple[str, str, dict]]) -> str:
    files: dict[str, dict] = {}
    has_more = False
    incomplete = False
    content_results = []
    for tool_name, data in results:
        if tool_name == "search_files":
            for file in data.get("files", []):
                files[file["id"]] = file
            has_more = has_more or data.get("has_more", False)
            incomplete = incomplete or data.get("incomplete_search", False)
        elif tool_name == "get_file":
            file = data.get("file")
            if file:
                files[file["id"]] = file
            content_results.append(data)

    lines = [f"Google Drive returned {len(files)} file(s)." if files else "No Google Drive files matched the search."]
    for file in files.values():
        details = [f"MIME type: {file['mime_type']}" if file.get("mime_type") else None,
                   f"Modified: {file['modified_time']}" if file.get("modified_time") else None,
                   f"Description: {file['description']}" if file.get("description") else None,
                   f"File ID: {file['id']}"]
        line = f"- {json.dumps(file['name'], ensure_ascii=False)} ({'; '.join(detail for detail in details if detail)})"
        if file.get("link"):
            line += f" — [Open file]({file['link']})"
        lines.append(line)
    for data in content_results:
        if data.get("content") is not None:
            lines.append(f"Content of {json.dumps(data['file']['name'], ensure_ascii=False)}:\n{data['content']}")
        elif data.get("content_status"):
            lines.append(data["content_status"])
    if has_more:
        lines.append("More matching Drive files are available beyond this result set.")
    if incomplete:
        lines.append("Google Drive marked this search incomplete; additional files may be missing.")
    for source, tool_name, data in other_results:
        lines.append(f"{source} ({tool_name}): {json.dumps(data, ensure_ascii=False)}")
    return "\n".join(lines)
