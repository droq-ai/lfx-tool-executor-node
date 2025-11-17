#!/usr/bin/env bash
set -euo pipefail

PORT="${1:-8005}"

echo "🚀 Starting LFx Tool Executor Node on port ${PORT}"

# Install local lfx package in editable mode so executor node uses latest code
if [ -d "lfx/src" ]; then
    echo "Installing local lfx package (editable mode)..."
    uv pip install -e ./lfx/src >/dev/null 2>&1 || true
fi

# Add lfx to PYTHONPATH so components can be imported directly (local source takes precedence)
export PYTHONPATH="$(pwd)/lfx/src:$(pwd)/src:${PYTHONPATH:-}"

uv run lfx-tool-executor-node "${PORT}"

