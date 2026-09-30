import asyncio
from datetime import datetime
from zoneinfo import ZoneInfo
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mcp import Client

from mcp_servers.calendar import mcp as calendar_mcp
from mcp_servers.drive import mcp as drive_mcp


async def main() -> bool:
    today = datetime.now(ZoneInfo(os.getenv("GOOGLE_TIMEZONE", "America/New_York"))).date().isoformat()
    success = True
    for source, server, tool, arguments, field in (
        ("Calendar", calendar_mcp, "get_events_by_date", {"day": today}, "events"),
        ("Drive", drive_mcp, "search_files", {"query": "project", "limit": 10}, "files"),
    ):
        async with Client(server) as client:
            result = await client.call_tool(tool, arguments)
            if result.is_error:
                details = " ".join(block.text for block in result.content if hasattr(block, "text"))
                print(f"{source} live check failed: {details}")
                success = False
                continue
            items = result.structured_content[field]
            print(f"{source} live check succeeded: {len(items)} {field} found.")
            for item in items[:5]:
                print("-", item.get("title") or item.get("name"))
    return success


if __name__ == "__main__":
    sys.exit(0 if asyncio.run(main()) else 1)
