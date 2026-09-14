#!/bin/bash
# Foreground entrypoint for the local mem0 HTTP server.
#
# FastMCP keeps the HTTP server in this process. Let launchd supervise that
# process directly so one unhealthy check cannot create overlapping children.
set -u
: "${BORG_HOME:?BORG_HOME is required}"

ulimit -n 8192 2>/dev/null || true
export MEM0_GRAPH_ENABLED="${MEM0_GRAPH_ENABLED:-1}" MEM0_GRAPH_IN_SEARCH="${MEM0_GRAPH_IN_SEARCH:-1}"
PY="${MEM0_SUPERVISOR_PY:-$BORG_HOME/mem0/venv/bin/python}"
SRV="${MEM0_SUPERVISOR_SERVER:-$BORG_HOME/mem0/bin/mem0-mcp-server-v2}"
cd "${MEM0_SUPERVISOR_BASE:-$BORG_HOME/mem0}" || exit 1

export MEM0_TELEMETRY=false
exec "$PY" "$SRV" --http "${BORG_MEMORY_PORT:?BORG_MEMORY_PORT is required}"
