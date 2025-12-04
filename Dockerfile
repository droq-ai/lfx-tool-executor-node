# Dockerfile template for Droq nodes
# This is an agnostic template - customize as needed for your node

FROM python:3.11-slim

# Set working directory
WORKDIR /app

# Install system dependencies
# Uncomment and add system packages as needed:
# RUN apt-get update && apt-get install -y \
#     gcc \
#     g++ \
#     make \
#     curl \
#     && rm -rf /var/lib/apt/lists/*

# Install uv
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

# Copy dependency files and source code
COPY pyproject.toml README.md ./
COPY uv.lock* ./
COPY src/ ./src/
COPY lfx /app/lfx
COPY node.json /app/node.json

# Install project dependencies
RUN uv pip install --system --no-cache -e .

# Create non-root user for security
RUN useradd -m -u 1000 nodeuser && chown -R nodeuser:nodeuser /app
USER nodeuser

# Set environment variables
ENV PYTHONPATH=/app/lfx/src:/app/src
ENV PYTHONUNBUFFERED=1

# Optional: Health check
# Uncomment and customize as needed:
# HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
#     CMD python -c "import sys; sys.exit(0)"

# Run the node
CMD ["uv", "run", "lfx-tool-executor-node"]

