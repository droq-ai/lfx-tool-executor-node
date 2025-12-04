"""Entry point for the LFx Tool Executor Node."""

from __future__ import annotations

import logging
import os
import sys

import uvicorn

from node.api import app

logger = logging.getLogger(__name__)


def _configure_logging() -> None:
    """Configure root logging for the executor node."""
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO"),
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    )


def main() -> None:
    """Launch the FastAPI application."""
    _configure_logging()

    host = os.getenv("HOST", "0.0.0.0")
    default_port = int(os.getenv("PORT", "8005"))
    port = int(sys.argv[1]) if len(sys.argv) > 1 else default_port

    logger.info("Starting LFx Tool Executor Node on %s:%s", host, port)
    uvicorn.run(app, host=host, port=port, log_level=os.getenv("UVICORN_LOG_LEVEL", "info"))


if __name__ == "__main__":
    main()
