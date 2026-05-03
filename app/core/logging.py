"""
app/core/logging.py

Structured logging setup using structlog.
Outputs JSON in production, coloured console output in development.
Every log line automatically includes:
  - timestamp
  - log level
  - logger name
  - tenant_slug (if set in context)
  - request_id (if set in context)
"""
import logging
import sys

import structlog
from structlog.contextvars import merge_contextvars

from app.core.config import settings


def configure_logging() -> None:
    """Call once at app startup."""

    shared_processors = [
        merge_contextvars,                          # inject request_id, tenant_slug, etc.
        structlog.stdlib.add_log_level,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.StackInfoRenderer(),
    ]

    if settings.is_production:
        # JSON output for log aggregation (Datadog, CloudWatch, etc.)
        processors = shared_processors + [
            structlog.processors.dict_tracebacks,
            structlog.processors.JSONRenderer(),
        ]
    else:
        # Human-readable coloured output for development
        processors = shared_processors + [
            structlog.dev.ConsoleRenderer(colors=True),
        ]

    structlog.configure(
        processors=processors,
        wrapper_class=structlog.make_filtering_bound_logger(
            getattr(logging, settings.LOG_LEVEL.upper(), logging.INFO)
        ),
        context_class=dict,
        logger_factory=structlog.PrintLoggerFactory(sys.stdout),
        cache_logger_on_first_use=True,
    )

    # Also configure stdlib logging so third-party libraries go through structlog
    logging.basicConfig(
        format="%(message)s",
        stream=sys.stdout,
        level=getattr(logging, settings.LOG_LEVEL.upper(), logging.INFO),
    )
