from pathlib import Path

import pytest

from kitten_bot.config import DEFAULT_MESSAGE, Config, validate_message


@pytest.fixture(autouse=True)
def clean_config_environment(monkeypatch):
    for key in ("BOT_TOKEN", "DB_PATH", "KITTEN_DIR", "LOG_LEVEL", "TELEGRAM_TIMEOUT"):
        monkeypatch.delenv(key, raising=False)


def test_default_message_matches_requested_text():
    assert DEFAULT_MESSAGE == (
        "Котеночки, вы уверены что хотите переписываться, а не созвониться?)"
    )


@pytest.mark.parametrize("token", [None, "", "   ", "paste_your_bot_token_here", "missing-colon"])
def test_missing_or_placeholder_token_is_rejected(monkeypatch, token):
    if token is not None:
        monkeypatch.setenv("BOT_TOKEN", token)
    with pytest.raises(ValueError, match="BOT_TOKEN"):
        Config.from_env()


def test_environment_defaults_and_token_are_loaded_without_leaking_repr(monkeypatch):
    token = "123456:fake-for-unit-tests"
    monkeypatch.setenv("BOT_TOKEN", f"  {token}  ")
    config = Config.from_env()
    assert config.token == token
    assert token not in repr(config)
    assert config.db_path == Path("data/bot.sqlite3")
    assert config.kitten_dir.is_dir()
    assert config.log_level == "INFO"
    assert config.telegram_timeout == 30


def test_paths_and_case_insensitive_log_level_can_be_overridden(monkeypatch, tmp_path):
    monkeypatch.setenv("BOT_TOKEN", "123456:fake-for-unit-tests")
    monkeypatch.setenv("DB_PATH", str(tmp_path / "custom.sqlite3"))
    monkeypatch.setenv("KITTEN_DIR", str(tmp_path / "pictures"))
    monkeypatch.setenv("LOG_LEVEL", "warning")
    config = Config.from_env()
    assert config.db_path == tmp_path / "custom.sqlite3"
    assert config.kitten_dir == tmp_path / "pictures"
    assert config.log_level == "WARNING"


@pytest.mark.parametrize("level", ["", "verbose", "warn", "100"])
def test_unsupported_log_level_is_rejected(monkeypatch, level):
    monkeypatch.setenv("BOT_TOKEN", "123456:fake-for-unit-tests")
    monkeypatch.setenv("LOG_LEVEL", level)
    with pytest.raises(ValueError, match="LOG_LEVEL"):
        Config.from_env()


@pytest.mark.parametrize("message", ["", " ", "\n\t", "а" * 1025, "🐱" * 513])
def test_empty_or_overlong_caption_is_rejected(message):
    with pytest.raises(ValueError, match="1024"):
        validate_message(message)


@pytest.mark.parametrize("message", ["а" * 1024, "🐱" * 512, "🐱" * 511 + "аб"])
def test_caption_limit_counts_utf16_units(message):
    assert validate_message(message) == message


def test_caption_trims_outer_whitespace_and_preserves_unicode_and_inner_newlines():
    message = "Созвонимся? 🐱\nВстреча в 18:00 — всем удобно?"
    assert validate_message(f" \n{message}\t ") == message


@pytest.mark.parametrize("value", ["0", "4.9", "121", "NaN", "inf", "", "slow"])
def test_invalid_network_timeout_is_rejected(monkeypatch, value):
    monkeypatch.setenv("BOT_TOKEN", "123456:fake-for-unit-tests")
    monkeypatch.setenv("TELEGRAM_TIMEOUT", value)
    with pytest.raises(ValueError, match="TELEGRAM_TIMEOUT"):
        Config.from_env()


def test_network_timeout_is_configurable(monkeypatch):
    monkeypatch.setenv("BOT_TOKEN", "123456:fake-for-unit-tests")
    monkeypatch.setenv("TELEGRAM_TIMEOUT", "60")
    assert Config.from_env().telegram_timeout == 60
