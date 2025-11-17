# LFx Tool Executor Node

A dedicated executor node for running Langflow tools inside the Droq distributed runtime.  
It exposes a lightweight FastAPI surface and will eventually host tool-specific logic (AgentQL, scraping helpers, etc.).

## Quick start

```bash
cd nodes/lfx-tool-executor-node
uv sync

# Run locally (defaults to port 8005)
./start-local.sh

# or specify a port
./start-local.sh 8015
```

The server exposes:

- `GET /health` – readiness probe
- `POST /api/v1/tools/run` – placeholder endpoint that will dispatch tool executions

## Configuration

Environment variables:

| Variable | Default | Description |
| --- | --- | --- |
| `HOST` | `0.0.0.0` | Bind address |
| `PORT` | `8005` | HTTP port when no CLI arg is supplied |
| `LOG_LEVEL` | `INFO` | Python logging level |

Additional secrets (API keys, service tokens) will be mounted per deployment as tools are added.

## Docker

```bash
docker build -t lfx-tool-executor-node:latest .
docker run --rm -p 8005:8005 lfx-tool-executor-node:latest
```

## Registering the node

After deploying, create/update the corresponding asset in `droq-node-registry` so workflows can discover this node and route tool components to it.

## License

Apache License 2.0
