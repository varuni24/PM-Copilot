#!/usr/bin/env bash
set -e

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BACKEND="$ROOT/backend"
FRONTEND="$ROOT/frontend"
LOGS="$ROOT/.logs"
mkdir -p "$LOGS"

# Ports
OLLAMA_PORT=11434
PG_MCP_PORT=8001
FASTAPI_PORT=8000
FRONTEND_PORT=3000
CALENDAR_PORT=8002
DRIVE_PORT=8003
SLACK_PORT=8004
NOTION_PORT=8005

# Kill anything we started when the script exits
PIDS=()
cleanup() {
  echo ""
  echo "Shutting down..."
  for pid in "${PIDS[@]}"; do
    kill "$pid" 2>/dev/null || true
  done
  wait 2>/dev/null || true
  echo "All stopped."
}
trap cleanup EXIT INT TERM

# Helper: run a command in the background, log to file, and remember PID
run() {
  local name="$1"; shift
  local log="$LOGS/$name.log"
  echo "Starting $name  → log: $log"
  "$@" > "$log" 2>&1 &
  PIDS+=($!)
}

# Helper: wait until a port answers
wait_for_port() {
  local name="$1" port="$2" tries=60
  echo -n "Waiting for $name on port $port "
  while ! nc -z 127.0.0.1 "$port" >/dev/null 2>&1; do
    ((tries--)) || { echo " FAILED (see $LOGS/$name.log)"; exit 1; }
    sleep 0.5
    echo -n "."
  done
  echo " ready."
}

# 1. PostgreSQL via Docker
echo "Starting PostgreSQL (docker)..."
(cd "$ROOT" && docker compose up -d db) > "$LOGS/docker.log" 2>&1
sleep 2

# 2. Ollama
if curl -s "http://127.0.0.1:$OLLAMA_PORT/api/tags" >/dev/null 2>&1; then
  echo "Ollama already running — skipping."
else
  run ollama ollama serve
  wait_for_port ollama "$OLLAMA_PORT"
fi

# 3. Backend Python services (share the venv)
source "$BACKEND/.venv/bin/activate"

run postgres-mcp bash -c "cd '$BACKEND' && python -m mcp_servers.postgres"
run fastapi       uvicorn app.main:app --reload --port "$FASTAPI_PORT" --app-dir "$BACKEND"

# 4. Optional MCP servers — only if their env vars are set
if grep -q '^CALENDAR_MCP_URL=' "$BACKEND/.env" 2>/dev/null; then
  run calendar-mcp bash -c "cd '$BACKEND' && python -m mcp_servers.calendar"
fi
if grep -q '^DRIVE_MCP_URL=' "$BACKEND/.env" 2>/dev/null; then
  run drive-mcp bash -c "cd '$BACKEND' && python -m mcp_servers.drive"
fi
if grep -q '^SLACK_MCP_URL=' "$BACKEND/.env" 2>/dev/null && grep -q '^SLACK_USER_TOKEN=..*' "$BACKEND/.env" 2>/dev/null; then
  run slack-mcp bash -c "cd '$BACKEND' && python -m mcp_servers.slack"
fi
if grep -q '^NOTION_MCP_URL=' "$BACKEND/.env" 2>/dev/null && grep -q '^NOTION_API_TOKEN=..*' "$BACKEND/.env" 2>/dev/null; then
  run notion-mcp bash -c "cd '$BACKEND' && python -m mcp_servers.notion"
fi

# 5. Frontend
run frontend npm --prefix "$FRONTEND" run dev

wait_for_port fastapi "$FASTAPI_PORT"
wait_for_port frontend "$FRONTEND_PORT"

echo ""
echo "──────────────────────────────────────────"
echo "  ✅ Everything is running"
echo "  App:      http://localhost:$FRONTEND_PORT"
echo "  Backend:  http://localhost:$FASTAPI_PORT/health"
echo "  Logs:     $LOGS/"
echo ""
echo "  Press Ctrl+C to stop everything."
echo "──────────────────────────────────────────"
echo ""

wait
