from __future__ import annotations

import pytest

from app.exceptions import ConfigurationError, RenameValidationError
from app.utils.validators import (
    parse_optional_int,
    validate_bot_token,
    validate_filename,
    validate_log_level,
)


def test_validate_bot_token_accepts_telegram_shape() -> None:
    assert validate_bot_token("123456789:abcdefghijklmnopqrstuvwxyzABCDE") is not None


def test_validate_bot_token_rejects_invalid_value() -> None:
    with pytest.raises(ConfigurationError):
        validate_bot_token("not-a-token")


def test_parse_optional_int() -> None:
    assert parse_optional_int("123", "EXAMPLE_ID") == 123
    assert parse_optional_int("", "EXAMPLE_ID") is None


def test_validate_log_level_normalizes() -> None:
    assert validate_log_level("info") == "INFO"


def test_validate_filename() -> None:
    assert validate_filename("report.pdf") == "report.pdf"
    with pytest.raises(RenameValidationError):
        validate_filename("../report.pdf")
