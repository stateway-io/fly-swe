from __future__ import annotations

import json

from issue_agent.diagnose.verdict import (
    SENTINEL_FILENAME,
    DiagnosisVerdict,
    consume_sentinel,
    parse_verdict,
    sanitize_rationale,
)


class TestConsumeSentinel:
    def test_reads_and_deletes_file(self, tmp_path) -> None:
        sentinel = tmp_path / SENTINEL_FILENAME
        sentinel.write_text('{"actual_category": "bug"}')

        result = consume_sentinel(tmp_path)

        assert result == '{"actual_category": "bug"}'
        assert not sentinel.exists()

    def test_missing_file_returns_none(self, tmp_path) -> None:
        assert consume_sentinel(tmp_path) is None

    def test_unreadable_content_still_deletes_file(self, tmp_path) -> None:
        sentinel = tmp_path / SENTINEL_FILENAME
        sentinel.write_bytes(b"\xff\xfe not utf-8 garbage")

        result = consume_sentinel(tmp_path)

        assert result is not None
        assert not sentinel.exists()


class TestParseVerdict:
    def test_valid_verdict(self) -> None:
        raw = json.dumps(
            {"actual_category": "bug", "confidence": "high", "rationale": "It's a bug."}
        )
        verdict = parse_verdict(raw)
        assert verdict == DiagnosisVerdict(
            actual_category="bug", confidence="high", rationale="It's a bug."
        )

    def test_none_input_returns_none(self) -> None:
        assert parse_verdict(None) is None

    def test_invalid_json_returns_none(self) -> None:
        assert parse_verdict("not json") is None

    def test_unknown_category_returns_none(self) -> None:
        raw = json.dumps({"actual_category": "not-a-category", "confidence": "high"})
        assert parse_verdict(raw) is None

    def test_unknown_confidence_returns_none(self) -> None:
        raw = json.dumps({"actual_category": "bug", "confidence": "very-sure"})
        assert parse_verdict(raw) is None

    def test_missing_actual_category_returns_none(self) -> None:
        raw = json.dumps({"confidence": "high"})
        assert parse_verdict(raw) is None

    def test_missing_confidence_returns_none(self) -> None:
        raw = json.dumps({"actual_category": "bug"})
        assert parse_verdict(raw) is None

    def test_missing_rationale_defaults_to_empty(self) -> None:
        raw = json.dumps({"actual_category": "bug", "confidence": "low"})
        verdict = parse_verdict(raw)
        assert verdict is not None
        assert verdict.rationale == ""

    def test_non_dict_json_returns_none(self) -> None:
        assert parse_verdict("[1, 2, 3]") is None

    def test_non_string_rationale_returns_none(self) -> None:
        raw = json.dumps({"actual_category": "bug", "confidence": "high", "rationale": 42})
        assert parse_verdict(raw) is None


class TestSanitizeRationale:
    def test_plain_text_passes_through(self) -> None:
        assert sanitize_rationale("This is a simple bug, not a feature.") == (
            "This is a simple bug, not a feature."
        )

    def test_removes_fenced_code_block_triple_backtick(self) -> None:
        text = "Look at this:\n```python\ndef foo():\n    pass\n```\nThat's the issue."
        result = sanitize_rationale(text)
        assert "def foo" not in result
        assert "```" not in result

    def test_removes_fenced_code_block_tilde(self) -> None:
        text = "See:\n~~~\ncode here\n~~~\ndone."
        result = sanitize_rationale(text)
        assert "code here" not in result

    def test_removes_four_space_indented_block(self) -> None:
        text = "Explanation:\n\n    def leaked():\n        return 1\n\nEnd."
        result = sanitize_rationale(text)
        assert "def leaked" not in result

    def test_removes_long_inline_backtick_span(self) -> None:
        long_code = "`" + "x = some_really_long_expression_that_looks_like_code()" + "`"
        text = f"The bug is here: {long_code} and that's it."
        result = sanitize_rationale(text)
        assert "some_really_long_expression" not in result

    def test_keeps_short_inline_backtick_span(self) -> None:
        text = "The file `utils.py` has the bug."
        result = sanitize_rationale(text)
        assert "`utils.py`" in result

    def test_truncates_to_max_chars(self) -> None:
        text = "a" * 2000
        result = sanitize_rationale(text, max_chars=100)
        assert len(result) <= 120  # allows for a truncation marker
        assert result.startswith("a" * 100)

    def test_empty_input_returns_empty(self) -> None:
        assert sanitize_rationale("") == ""

    def test_whitespace_only_after_stripping_returns_empty(self) -> None:
        text = "```\nall of this is code\n```"
        assert sanitize_rationale(text) == ""
