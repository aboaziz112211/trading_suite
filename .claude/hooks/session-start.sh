#!/bin/bash
set -euo pipefail

# SessionStart hook for Claude Code on the web.
# Installs dependencies so tests/linters work in remote sessions.
# Safe to run repeatedly (idempotent) and does nothing on local machines.

if [ "${CLAUDE_CODE_REMOTE:-}" != "true" ]; then
  exit 0
fi

cd "$CLAUDE_PROJECT_DIR"

# Python deps for the Flask app (app.py)
pip install -r requirements.txt

# Node deps for the production/ assistant service
if [ -f production/package.json ]; then
  (cd production && npm install --no-audit --no-fund)
fi

echo "Session start hook: dependencies installed."
