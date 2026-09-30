import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from integrations.google_oauth import authenticate

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Authenticate a local read-only Google MCP server in your browser.")
    parser.add_argument("source", choices=["calendar", "drive"])
    args = parser.parse_args()
    try:
        authenticate(args.source)
    except RuntimeError as exc:
        print(f"Authentication cannot start: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
    print(f"Google {args.source} authentication complete. Local token saved.")
