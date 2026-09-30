# PM Copilot

An AI project-management copilot that answers questions across Slack, Google Calendar, Google Drive, Notion, and a PostgreSQL project database—with one local Qwen model orchestrating tools via MCP.

## What it does

- "What tasks are overdue?"
- "What happened in Project A this week?"
- "Find the Project B project description."
- "Create a task for Sarah to prepare the stakeholder update by Friday."

## Screenshot

![PM Copilot main page](images/main-page.png)
![PM Copilot chat example](images/chat-eg.png)

## Architecture

```text
Next.js → FastAPI → Ollama/Qwen → MCP client ┬→ PostgreSQL MCP → PostgreSQL
                                             ├→ Calendar MCP → Google Calendar
                                             ├→ Drive MCP → Google Drive
                                             ├→ Slack MCP → Slack
                                             └→ Notion MCP → Notion
```

One local model decides which MCP tools to use; each integration is a separate read-only MCP server.

## Quick start

Requirements: Docker Desktop, Ollama, Python 3.10 or newer, Node.js 20 or newer, and npm.

From the repository root:

```bash
docker compose up -d db
ollama pull qwen3:4b-instruct
python3 -m venv backend/.venv
source backend/.venv/bin/activate
pip install -r backend/requirements.txt
cp backend/.env.example backend/.env   # then edit tokens
python backend/scripts/init_db.py
npm --prefix frontend install
bash start.sh
```

Open [http://localhost:3000](http://localhost:3000). `start.sh` starts PostgreSQL, Ollama when needed, FastAPI, the configured MCP servers, and the frontend. Press `Ctrl+C` to stop the processes started by the script.

`python backend/scripts/init_db.py` creates the schema and seeds sample projects, tasks, people, decisions, and risks so the PostgreSQL-backed questions return data immediately. It is safe to rerun.

## Configuration

| File                  | Variable                     | Purpose                                                                                                                                      |
| --------------------- | ---------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------- |
| `backend/.env`        | `OLLAMA_BASE_URL`            | Local Ollama server URL; defaults to `http://localhost:11434`.                                                                               |
| `backend/.env`        | `OLLAMA_MODEL`               | Pulled local model name; defaults to `qwen3:4b-instruct`. If you change it, run `ollama pull <model-name>`.                                  |
| `backend/.env`        | `DATABASE_URL`               | SQLAlchemy connection string, defaulting to `postgresql+psycopg://pm_copilot:pm_copilot@localhost:5432/pm_copilot` for the Compose database. |
| `backend/.env`        | `MCP_URL`                    | MCP server endpoint; defaults to `http://127.0.0.1:8001/mcp`.                                                                                |
| `backend/.env`        | `CALENDAR_MCP_URL`           | Optional Calendar MCP endpoint, `http://127.0.0.1:8002/mcp`.                                                                                 |
| `backend/.env`        | `DRIVE_MCP_URL`              | Optional Drive MCP endpoint, `http://127.0.0.1:8003/mcp`.                                                                                    |
| `backend/.env`        | `SLACK_MCP_URL`              | Optional Slack MCP endpoint, `http://127.0.0.1:8004/mcp`.                                                                                    |
| `backend/.env`        | `NOTION_MCP_URL`             | Optional Notion MCP endpoint, `http://127.0.0.1:8005/mcp`.                                                                                   |
| `backend/.env`        | `SLACK_USER_TOKEN`           | Slack User OAuth Token, kept only in ignored `backend/.env`.                                                                                 |
| `backend/.env`        | `SLACK_INCLUDE_DMS`          | Optional, defaults to `false`; set `true` only to attempt separate DM and group-DM listing with their read scopes.                           |
| `backend/.env`        | `PM_CONTEXT_FILE`            | Backend PM context JSON path; defaults to `integrations/project_context.json` and is read by FastAPI, not the Slack MCP server.              |
| `backend/.env`        | `NOTION_API_TOKEN`           | Notion read-content connection secret, kept only in ignored `backend/.env`.                                                                  |
| `backend/.env`        | `GOOGLE_CLIENT_SECRETS_FILE` | Local downloaded Desktop OAuth client JSON; defaults to `.local/google/client_secret.json`.                                                  |
| `backend/.env`        | `GOOGLE_CALENDAR_TOKEN_FILE` | Ignored local Calendar OAuth token; defaults to `.local/google/calendar_token.json`.                                                         |
| `backend/.env`        | `GOOGLE_DRIVE_TOKEN_FILE`    | Ignored local Drive OAuth token; defaults to `.local/google/drive_token.json`.                                                               |
| `backend/.env`        | `GOOGLE_TIMEZONE`            | Local timezone for today/tomorrow Calendar queries; defaults to `America/New_York`.                                                          |
| `backend/.env`        | `GOOGLE_CALENDAR_ID`         | Calendar to read; defaults to `primary`.                                                                                                     |
| `backend/.env`        | `FRONTEND_ORIGIN`            | Browser origin allowed by backend CORS; defaults to `http://localhost:3000`.                                                                 |
| `frontend/.env.local` | `NEXT_PUBLIC_API_URL`        | Backend URL the browser calls; defaults to `http://localhost:8000`.                                                                          |

## Integrations

### PostgreSQL

The Compose service provides the local project database. Run `docker compose up -d db`, then `backend/scripts/init_db.py` to create the schema and seed sample projects, tasks, people, decisions, and risks. The PostgreSQL MCP server exposes project-management tools, including task lookup and task creation; the assistant resolves a real project ID before creating a task.

```bash
source backend/.venv/bin/activate
python backend/scripts/init_db.py
```

### Google Calendar

In [Google Cloud Console](https://console.cloud.google.com/), create or select a project, enable the Google Calendar API, configure the OAuth consent screen, and add your account as a test user when the app is External and in Testing. Add the `https://www.googleapis.com/auth/calendar.readonly` scope, create a Desktop app OAuth client, and save its downloaded JSON as `backend/.local/google/client_secret.json`. Authenticate in a browser with `python backend/scripts/authenticate_google.py calendar`. The Calendar MCP server reads the configured calendar and uses `GOOGLE_TIMEZONE` for relative dates; it does not edit or delete events.

### Google Drive

Enable the Google Drive API in the same Google Cloud project and add the restricted read-only scope `https://www.googleapis.com/auth/drive.readonly` to the consent screen. Save the Desktop OAuth client as described above, ensure your account is an allowed test user, then run `python backend/scripts/authenticate_google.py drive`. Drive searches My Drive, files shared with the authenticated user, and accessible organization Shared drives. The integration reads file metadata and supported content without creating, editing, or deleting files.

If Google returns `access_denied` or `403`, verify that the API is enabled, the account is listed as a test user, and the requested read-only scope is configured. After changing a client or scope, delete the corresponding ignored token JSON and authenticate again.

### Slack

Create a Slack app for the target workspace and install it with a User OAuth Token. Under **OAuth & Permissions → User Token Scopes**, use `search:read`, `channels:read`, and `users:read`; add `groups:read` for joined private channels and the matching `channels:history` or `groups:history` scope when channel history is needed. Put the resulting `xoxp-...` token in `backend/.env` as `SLACK_USER_TOKEN`. The server restricts results to joined, non-archived channels and uses `users:read` to resolve message authors and mentions. It never sends, edits, or deletes messages.

DM support is optional and disabled by default. Set `SLACK_INCLUDE_DMS=true` only when needed, then grant `im:read` and `mpim:read`; reading their history also needs `im:history` and `mpim:history`. Missing optional DM scopes do not stop normal channel search. Workspace policy may require administrator approval or app reinstallation after scope changes.

### Notion

Create a [Notion internal connection](https://developers.notion.com/guides/get-started/internal-connections) with **Read content** capability only. Explicitly share the pages and databases the copilot should access through Notion's Connections controls, then put the connection secret in `backend/.env` as `NOTION_API_TOKEN`. Search covers titles shared with the connection; page retrieval reads accessible properties and top-level blocks. The integration cannot create, edit, or delete Notion content.

## Tests

Automated tests use mocked Google, Slack, Notion, and model responses, so external credentials are not required. Database tests should use a separate PostgreSQL database:

```bash
docker compose up -d db
docker compose exec db createdb -U pm_copilot pm_copilot_test
cd backend
source .venv/bin/activate
DATABASE_URL='postgresql+psycopg://pm_copilot:pm_copilot@localhost:5432/pm_copilot_test' python -m unittest discover -s tests -v
```

Live checks make real, read-only API calls and require the corresponding credentials in `backend/.env`:

```bash
cd backend
source .venv/bin/activate
python scripts/live_google_check.py
python scripts/live_slack_notion_check.py both
```

A successful live check may return zero matches. Authentication, missing-scope, and API failures are reported explicitly.

## Stack

- Next.js 16 + React 19 + TypeScript
- FastAPI + Python 3.10+
- Ollama (Qwen3 4B)—local LLM, no API keys
- MCP Python SDK
- SQLAlchemy + PostgreSQL
- Google API Python client
- Slack Web API
- Notion API

## Acknowledgments

Built as a personal learning project, with OpenAI Codex used for code generation assistance. Architecture, integration setup, testing, and debugging were done manually.
