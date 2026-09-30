"""Read-only live authentication/API check; never prints tokens."""

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv
from mcp.server.mcpserver.exceptions import ToolError

from mcp_servers.notion import search_databases, search_pages
from mcp_servers.slack import _api, get_channels, search_messages

load_dotenv()


def check_slack() -> bool:
    if not os.getenv("SLACK_USER_TOKEN"):
        print("Slack FAIL: SLACK_USER_TOKEN is missing from backend/.env.")
        return False
    try:
        identity = _api("auth.test", http_method="POST")
        messages = search_messages("DS701", 5)
        channels = get_channels(5)
    except ToolError as exc:
        print(f"Slack FAIL: {exc}")
        return False
    print(f"Slack OK: authenticated to {identity.get('team', 'the authorized workspace')}; "
          f"read {len(messages.messages)} DS701 search matches and {len(channels.channels)} joined conversations.")
    return True


def check_notion() -> bool:
    if not os.getenv("NOTION_API_TOKEN"):
        print("Notion FAIL: NOTION_API_TOKEN is missing from backend/.env.")
        return False
    try:
        pages = search_pages("DS701", 5)
        data_sources = search_databases("", 5)
    except ToolError as exc:
        print(f"Notion FAIL: {exc}")
        return False
    print(f"Notion OK: read {len(pages.pages)} DS701 title matches and "
          f"{len(data_sources.data_sources)} accessible data sources.")
    if not pages.pages and not data_sources.data_sources:
        print("Notion note: zero accessible results. Share a page/database with the connection, then rerun.")
    return True


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Check live read-only Slack and/or Notion access.")
    parser.add_argument("source", choices=("slack", "notion", "both"), default="both", nargs="?")
    selected = parser.parse_args().source
    results = []
    if selected in ("slack", "both"):
        results.append(check_slack())
    if selected in ("notion", "both"):
        results.append(check_notion())
    raise SystemExit(0 if all(results) else 1)
