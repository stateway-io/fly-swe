from __future__ import annotations

import json

import pytest
import structlog

from issue_agent.logging import bind_context, get_logger, setup_logging


class TestSetup:
    def teardown_method(self):
        structlog.reset_defaults()

    def test_setup_logging_default(self, capsys):
        setup_logging()
        logger = get_logger()
        logger.info("test event")
        out, _ = capsys.readouterr()
        parsed = json.loads(out.strip())
        assert parsed["event"] == "test event"

    def test_json_output_in_prod(self, capsys):
        setup_logging("info")
        logger = get_logger()
        logger.info("hello")
        out, _ = capsys.readouterr()
        parsed = json.loads(out.strip())
        assert parsed["event"] == "hello"
        assert parsed["level"] == "info"
        assert parsed["service"] == "issue-agent"
        assert "timestamp" in parsed

    def test_service_name_reflects_configured_bot_name(self, capsys):
        setup_logging("info", service_name="My Custom Bot")
        logger = get_logger()
        logger.info("hello")
        out, _ = capsys.readouterr()
        parsed = json.loads(out.strip())
        assert parsed["service"] == "My Custom Bot"

    def test_console_output_in_debug(self, capsys):
        setup_logging("debug")
        logger = get_logger()
        logger.info("hello there")
        out, _ = capsys.readouterr()
        assert "hello there" in out
        with pytest.raises((json.JSONDecodeError, ValueError)):
            line = out.strip().splitlines()[0]
            json.loads(line)

    def test_level_filtering_suppresses_lower(self, capsys):
        setup_logging("warning")
        logger = get_logger()
        logger.info("should be silent")
        out, _ = capsys.readouterr()
        assert out.strip() == ""

    def test_level_filtering_passes_match(self, capsys):
        setup_logging("warning")
        logger = get_logger()
        logger.warning("warning msg")
        out, _ = capsys.readouterr()
        parsed = json.loads(out.strip())
        assert parsed["event"] == "warning msg"


class TestBindContext:
    def teardown_method(self):
        structlog.reset_defaults()

    def test_bind_task_context(self, capsys):
        setup_logging("info")
        logger = get_logger()
        bound = bind_context(
            logger, task_id="abc-123", repo="stateway/stateway"
        )
        bound.info("context test")
        out, _ = capsys.readouterr()
        parsed = json.loads(out.strip())
        assert parsed["task_id"] == "abc-123"
        assert parsed["repo"] == "stateway/stateway"

    def test_bind_does_not_mutate_original(self, capsys):
        setup_logging("info")
        logger = get_logger()
        bind_context(logger, task_id="abc-123")
        logger.info("original only")
        out, _ = capsys.readouterr()
        parsed = json.loads(out.strip())
        assert "task_id" not in parsed


class TestGetLogger:
    def teardown_method(self):
        structlog.reset_defaults()

    def test_get_logger_has_log_methods(self):
        setup_logging("info")
        logger = get_logger()
        assert hasattr(logger, "info")
        assert hasattr(logger, "error")
        assert hasattr(logger, "warn")
        assert hasattr(logger, "bind")

    def test_get_logger_with_name_works(self):
        setup_logging("info")
        logger = get_logger("custom-name")
        assert hasattr(logger, "info")
