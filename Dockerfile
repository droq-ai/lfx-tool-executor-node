# syntax=docker/dockerfile:1
# Dockerfile for LFX Tool Executor Node
# Build from repo root: docker build -f Dockerfile -t droqai/lfx-tool-executor-node:latest .

################################
# BUILDER STAGE
# Build dependencies and Langflow
################################
FROM ghcr.io/astral-sh/uv:python3.12-alpine AS builder

# Install build dependencies
# Retry on failure to handle transient network issues
RUN set -e; \
    for i in 1 2 3; do \
        apk update && \
        apk add --no-cache \
            build-base \
            libaio-dev \
            linux-headers && \
        break || sleep 5; \
    done

WORKDIR /app

# Enable bytecode compilation
ENV UV_COMPILE_BYTECODE=1
ENV UV_LINK_MODE=copy

# Copy dependency files first (for better caching)
# Copy uv.lock file to use the same dependency resolution as local setup
COPY uv.lock /app/uv.lock
COPY pyproject.toml /app/pyproject.toml
COPY README.md /app/README.md

# Copy Langflow dependency files
COPY lfx/pyproject.toml /app/lfx/pyproject.toml
COPY lfx/README.md /app/lfx/README.md

# Copy Langflow source (needed for installation)
COPY lfx/src /app/lfx/src

# Copy executor node source
COPY src/node /app/src/node

# Create venv first
RUN --mount=type=cache,target=/root/.cache/uv \
    uv venv

# Install lfx package FIRST to pin langchain-core<1.0.0 (which has langchain_core.memory)
# This ensures langchain_core.memory is available before any other packages
RUN --mount=type=cache,target=/root/.cache/uv \
    cd /app/lfx && \
    uv pip install --python /app/.venv/bin/python --no-cache -e .

# Explicitly pin langchain-core to <1.0.0 to prevent any upgrades
# This is critical - langchain-core>=1.0.0 doesn't have langchain_core.memory
RUN --mount=type=cache,target=/root/.cache/uv \
    uv pip install --python /app/.venv/bin/python --no-cache \
    "langchain-core>=0.3.66,<1.0.0" --force-reinstall

# Install langchain package which provides langchain_core.memory
RUN --mount=type=cache,target=/root/.cache/uv \
    uv pip install --python /app/.venv/bin/python --no-cache \
    "langchain~=0.3.23"

# Now sync other dependencies from lock file (executor node dependencies)
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev || echo "Warning: Some dependencies may conflict, continuing..."

# Install the executor node package itself (editable install like local)
# Note: langchain-chroma and langchain-docling may fail on Alpine aarch64 due to missing wheels
# Install package without these problematic dependencies first, then try to install them separately (allow failure)
RUN --mount=type=cache,target=/root/.cache/uv \
    cd /app && \
    uv pip install --python /app/.venv/bin/python --no-cache -e . --no-deps && \
    uv pip install --python /app/.venv/bin/python --no-cache \
        "fastapi>=0.115.0,<1.0.0" \
        "uvicorn[standard]>=0.34.0,<1.0.0" \
        "pydantic>=2.0.0,<3.0.0" \
        "python-dotenv>=1.0.0,<2.0.0" \
        "structlog>=25.0.0,<26.0.0" \
        "nats-py>=2.6.0,<3.0.0" \
        "httpx>=0.27.0,<1.0.0" \
        "langchain-core>=0.3.79,<0.4.0" \
        "langchain==0.3.23" \
        "langchain-anthropic==0.3.14" \
        "langchain-astradb>=0.6.1,<1.0.0" \
        "langchain-aws==0.2.33" \
        "langchain-cohere==0.3.3" \
        "langchain-community>=0.3.21,<1.0.0" \
        "langchain-elasticsearch==0.3.0" \
        "langchain-google-calendar-tools==0.0.1" \
        "langchain-google-community==2.0.3" \
        "langchain-google-genai==2.0.6" \
        "langchain-google-vertexai>=2.0.7,<3.0.0" \
        "langchain-graph-retriever==0.8.0" \
        "langchain-groq==0.2.1" \
        "langchain-huggingface==0.3.1" \
        "langchain-milvus==0.1.7" \
        "langchain-mistralai==0.2.3" \
        "langchain-mongodb==0.7.0" \
        "langchain-nvidia-ai-endpoints==0.3.8" \
        "langchain-ollama==0.2.1" \
        "langchain-openai>=0.2.12,<1.0.0" \
        "langchain-pinecone>=0.2.8,<1.0.0" \
        "langchain-sambanova==0.1.0" \
        "langchain-unstructured>=0.1.5" \
        "langchain-ibm>=0.3.8" \
        "nanoid>=2.0.0" && \
    (uv pip install --python /app/.venv/bin/python --no-cache "langchain-chroma>=0.2.6,<1.0.0" || \
     echo "Warning: langchain-chroma installation failed (onnxruntime may not have Python 3.12 wheels for Alpine aarch64)") && \
    (uv pip install --python /app/.venv/bin/python --no-cache "langchain-docling>=1.1.0" || \
     echo "Warning: langchain-docling installation failed (torch may not have Python 3.12 wheels for Alpine aarch64)")

# Install langchain integration packages compatible with langchain-core<1.0.0
# Pin versions to match local working installation (langchain-core 0.3.79, pydantic 2.12.4)
RUN --mount=type=cache,target=/root/.cache/uv \
    uv pip install --python /app/.venv/bin/python --no-cache \
    "langchain-openai==0.2.14" \
    "langchain-anthropic>=0.1.13,<0.2.0" \
    "langchain-community>=0.0.38,<0.1.0" \
    "langchain-google-genai>=0.0.6,<0.1.0" \
    "langchain-ollama>=0.3.5,<0.4.0" || echo "Warning: Some langchain packages failed to install"

# Re-pin langchain-core after installing integration packages (they might try to upgrade it)
RUN --mount=type=cache,target=/root/.cache/uv \
    uv pip install --python /app/.venv/bin/python --no-cache \
    "langchain-core>=0.3.66,<1.0.0" --force-reinstall

# Re-install langchain package after re-pinning langchain-core (uv sync or re-pin might have removed it)
RUN --mount=type=cache,target=/root/.cache/uv \
    uv pip install --python /app/.venv/bin/python --no-cache \
    "langchain~=0.3.23" || echo "Warning: langchain installation failed"

# Re-install langchain-openai after re-pinning to ensure compatibility with langchain-core<1.0.0
RUN --mount=type=cache,target=/root/.cache/uv \
    uv pip install --python /app/.venv/bin/python --no-cache \
    "langchain-openai==0.2.14" --force-reinstall || echo "Warning: langchain-openai re-installation failed"

# Ensure all lfx dependencies are installed (some might not be in the lock file)
# NOTE: aiofile and aiofiles are DIFFERENT packages - both are required!
# Also ensure langchain is installed (required by AgentComponent and other components)
RUN --mount=type=cache,target=/root/.cache/uv \
    uv pip install --python /app/.venv/bin/python --no-cache \
    "langchain~=0.3.23" \
    "nanoid>=2.0.0,<3.0.0" \
    "platformdirs>=4.3.8,<5.0.0" \
    "aiofile>=3.8.0,<4.0.0" \
    "aiofiles>=24.1.0,<25.0.0" \
    "pillow>=10.0.0,<13.0.0" \
    "emoji>=2.0.0,<3.0.0" \
    "asyncer>=0.0.8,<1.0.0" \
    "cachetools>=5.5.2,<6.0.0" \
    "chardet>=5.2.0,<6.0.0" \
    "defusedxml>=0.7.1,<1.0.0" \
    "docstring-parser>=0.16,<1.0.0" \
    "json-repair>=0.30.3,<1.0.0" \
    "loguru>=0.7.3,<1.0.0" \
    "networkx>=3.4.2,<4.0.0" \
    "orjson>=3.10.15,<4.0.0" \
    "passlib>=1.7.4,<2.0.0" \
    "pydantic>=2.12.4,<3.0.0" \
    "pydantic-settings>=2.10.1,<3.0.0" \
    "rich>=13.0.0,<14.0.0" \
    "tomli>=2.2.1,<3.0.0" \
    "typer>=0.16.0,<1.0.0" \
    "typing-extensions>=4.14.0,<5.0.0" \
    "validators>=0.34.0,<1.0.0" \
    "qdrant-client>=1.15.1,<2.0.0"

# Install langchain-experimental for PythonREPLComponent and other experimental features
RUN --mount=type=cache,target=/root/.cache/uv \
    uv pip install --python /app/.venv/bin/python --no-cache \
    "langchain-experimental>=0.0.50,<1.0.0" || echo "Warning: langchain-experimental installation failed"

# Re-pin langchain-core after langchain-experimental (it might have upgraded langchain-core to >=1.0.0)
RUN --mount=type=cache,target=/root/.cache/uv \
    uv pip install --python /app/.venv/bin/python --no-cache \
    "langchain-core>=0.3.66,<1.0.0" --force-reinstall

# Copy node.json mapping file and startup script
COPY node.json /app/node.json
COPY start-local.sh /app/start-local.sh

################################
# RUNTIME STAGE
# Minimal runtime image
################################
FROM python:3.12-alpine AS runtime

# Install runtime dependencies with retry (including bash for start-local.sh)
RUN set -e; \
    for i in 1 2 3; do \
        apk update && \
        apk add --no-cache curl bash && \
        break || sleep 5; \
    done

# Create non-root user
RUN adduser -D -u 1000 -G root -h /app -s /sbin/nologin executor

WORKDIR /app

# Copy the virtual environment from builder (created by uv sync)
# This ensures all dependencies are installed exactly as in local setup
COPY --from=builder --chown=executor:root /app/.venv /app/.venv

# Copy application code
# Copy node package to /app/node (so "import node" works)
COPY --from=builder --chown=executor:root /app/src/node /app/node
COPY --from=builder --chown=executor:root /app/lfx/src /app/lfx/src
COPY --from=builder --chown=executor:root /app/node.json /node.json
COPY --from=builder --chown=executor:root /app/README.md /app/README.md
COPY --from=builder --chown=executor:root /app/pyproject.toml /app/pyproject.toml
COPY --from=builder /app/start-local.sh /app/start-local.sh

# Add venv to PATH so Python uses the venv's packages
ENV PATH="/app/.venv/bin:$PATH"

# Make startup script executable and verify it exists (as root, before switching users)
RUN chmod +x /app/start-local.sh && \
    chown executor:root /app/start-local.sh && \
    ls -la /app/start-local.sh

# Set environment variables
ENV PYTHONPATH=/app:/app/lfx/src
ENV PYTHONUNBUFFERED=1
ENV HOST=0.0.0.0
ENV PORT=8005
ENV LANGFLOW_EXECUTOR_NODE_URL=http://localhost:8005
ENV DOCKER_CONTAINER=1
ENV RELOAD=false

# Switch to non-root user
USER executor

# Expose port
EXPOSE 8005

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD curl -f http://localhost:8005/health || exit 1

# Run the executor node using start-local.sh
CMD ["/bin/bash", "./start-local.sh"]