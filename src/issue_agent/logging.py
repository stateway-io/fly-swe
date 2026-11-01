from __future__ import annotations

import logging
from functools import partial

import structlog

# Fallback used when a caller doesn't pass service_name (e.g. tests that
# exercise setup_logging() in isolation). Real startup paths (main.py,
# worker.py) always pass settings.agent_name explicitly.
_DEFAULT_SERVICE_NAME = "issue-agent"


def _add_service_meta(
    service_name: str,
    logger: structlog.BoundLogger,
    method_name: str,
    event_dict: dict,
) -> dict:
    event_dict["service"] = service_name
    return event_dict


def setup_logging(log_level: str = "info", service_name: str = _DEFAULT_SERVICE_NAME) -> None:
    # NOTE: intentionally does NOT import get_settings() here — logging.py is
    # imported very early in the app's startup, and pulling in config.py from
    # here would risk a circular import. Callers pass the configured
    # bot name in explicitly instead (see main.py / worker.py).
    level = getattr(logging, log_level.upper(), logging.INFO)

    processors: list[structlog.types.Processor] = [
        structlog.stdlib.add_log_level,
        partial(_add_service_meta, service_name),
        structlog.processors.TimeStamper(fmt="iso", utc=True),
    ]

    if log_level == "debug":
        processors.append(structlog.dev.ConsoleRenderer())
    else:
        processors.append(structlog.processors.JSONRenderer())

    structlog.configure(
        processors=processors,
        wrapper_class=structlog.make_filtering_bound_logger(level),
        context_class=dict,
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )


def get_logger(*args, **kwargs) -> structlog.BoundLogger:
    return structlog.get_logger(*args, **kwargs)


def bind_context(logger: structlog.BoundLogger, **kwargs) -> structlog.BoundLogger:
    return logger.bind(**kwargs)
