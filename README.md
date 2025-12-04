# LFX Tool Executor Node

**LFX Tool Executor Node** provides a unified interface for running LangFlow tools inside the Droq distributed runtime

## 🚀 Installation

### Using UV (Recommended)

```bash
# Install UV
curl -LsSf https://astral.sh/uv/install.sh | sh

# Clone and setup
git clone https://github.com/droq-ai/lfx-tool-executor-node.git
cd lfx-tool-executor-node
uv sync

# Verify installation
uv run lfx-tool-executor-node --help
```

### Using Docker

```bash
docker build -t lfx-tool-executor-node:latest .
docker run --rm -p 8005:8005 lfx-tool-executor-node:latest
```

## 🧩 Usage

### Running the Node

```bash
# Run locally (defaults to port 8005)
./start-local.sh

# or specify a port
./start-local.sh 8005

# or use uv directly
uv run lfx-tool-executor-node --port 8005
```

### API Endpoints

The server exposes:

- `GET /health` – readiness probe
- `POST /api/v1/execute` – execute specific tools

## ⚙️ Configuration

Environment variables:

| Variable | Default | Description |
| --- | --- | --- |
| `HOST` | `0.0.0.0` | Bind address |
| `PORT` | `8005` | HTTP port |
| `LOG_LEVEL` | `INFO` | Python logging level |
| `NODE_ID` | `lfx-tool-executor-node` | Node identifier |

### Component Categories

## 🔧 Development

```bash
# Install development dependencies
uv sync --group dev

# Run tests
uv run pytest

# Format code
uv run black src/ tests/
uv run ruff check src/ tests/
uv run ruff format src/ tests/

# Type checking
uv run mypy src/
```

## 🤝 Contributing

Please read our [Contributing Guide](CONTRIBUTING.md) for details on our code of conduct and the process for submitting pull requests.

## 📄 License

This project is licensed under the Apache License 2.0 - see the [LICENSE](LICENSE) file for details.

## 🔗 Related Projects

- [Droq Node Registry](https://github.com/droq-ai/droq-node-registry) - Node discovery and registration
- [Langflow](https://github.com/langflow-ai/langflow) - Visual AI workflow builder
