#!/usr/bin/env bash
set -euo pipefail

PORT="${1:-8005}"

echo "🚀 Starting LFx Tool Executor Node on port ${PORT}"

# Check if running in Docker
if [ -n "${DOCKER_CONTAINER:-}" ]; then
    echo "Running in Docker container - using venv Python directly"
    # Use the venv that was copied from the builder stage
    if [ -d "/app/.venv" ]; then
        export PATH="/app/.venv/bin:$PATH"
        PYTHON_CMD="/app/.venv/bin/python"
    else
        PYTHON_CMD="python"
    fi
    
    # Set PYTHONPATH (already set in Dockerfile, but ensure it's correct)
    export PYTHONPATH="/app:/app/lfx/src:${PYTHONPATH:-}"
    
    # Run using uvicorn directly (like runtime executor node)
    cd /app
    exec $PYTHON_CMD -m uvicorn node.api:app --host "${HOST:-0.0.0.0}" --port "${PORT}" --log-level "${LOG_LEVEL:-info}"
else
    # Local development - check if uv is installed
    if ! command -v uv >/dev/null 2>&1; then
        echo "Error: uv is not installed. Please install it first:"
        echo "  pipx install uv"
        echo "  or visit: https://github.com/astral-sh/uv"
        exit 1
    fi
    
    # Install local lfx package in editable mode so executor node uses latest code
    if [ -d "lfx/src" ]; then
        echo "Installing local lfx package (editable mode)..."
        uv pip install -e ./lfx/src >/dev/null 2>&1 || true
    fi
    
    # Add lfx to PYTHONPATH so components can be imported directly (local source takes precedence)
    export PYTHONPATH="$(pwd)/lfx/src:$(pwd)/src:${PYTHONPATH:-}"
    
    uv run lfx-tool-executor-node "${PORT}"
fi

