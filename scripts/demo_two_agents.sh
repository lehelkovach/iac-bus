#!/usr/bin/env bash
# Two agents, one file: lock hand-over through the MCP adapter.
#
#   bash scripts/demo_two_agents.sh                 # starts a throwaway bus on :8102
#   BUS_URL=http://127.0.0.1:8101 BUS_API_TOKEN=devtoken bash scripts/demo_two_agents.sh
#
# Needs the MCP SDK: pip install -r requirements-mcp.txt (or requirements-dev.txt).
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${ROOT}"

if [[ -x "${ROOT}/venv/bin/python" ]]; then
  PYTHON="${PYTHON:-${ROOT}/venv/bin/python}"
else
  PYTHON="${PYTHON:-python3}"
fi

if ! "${PYTHON}" -c "import mcp" 2>/dev/null; then
  echo "The MCP SDK is not installed for ${PYTHON}. Run: pip install -r requirements-mcp.txt" >&2
  exit 1
fi

BUS_PID=""
cleanup() {
  if [[ -n "${BUS_PID}" ]]; then
    kill "${BUS_PID}" 2>/dev/null || true
  fi
}
trap cleanup EXIT

if [[ -z "${BUS_URL:-}" ]]; then
  DEMO_BUS_PORT="${DEMO_BUS_PORT:-8102}"
  export BUS_URL="http://127.0.0.1:${DEMO_BUS_PORT}"
  export BUS_API_TOKEN="${BUS_API_TOKEN:-demotoken}"
  BUS_PORT="${DEMO_BUS_PORT}" BUS_HOST=127.0.0.1 BUS_LOG_LEVEL=WARNING \
    "${PYTHON}" server.py >/dev/null 2>&1 &
  BUS_PID=$!
  for _ in $(seq 1 50); do
    if curl -fsS "${BUS_URL}/health" >/dev/null 2>&1; then
      break
    fi
    sleep 0.1
  done
  if ! curl -fsS "${BUS_URL}/health" >/dev/null 2>&1; then
    echo "Could not start a local bus on ${BUS_URL}" >&2
    exit 1
  fi
fi

export BUS_URL BUS_API_TOKEN="${BUS_API_TOKEN:-}"
# The MCP servers log a startup line and refused tool calls to stderr; the
# transcript already shows both, so keep stderr for real errors only.
"${PYTHON}" scripts/demo_two_agents.py 2> >(grep -v -E "^(iac-bus MCP adapter:|Tool '[a-z_]+' failed:)" >&2)
