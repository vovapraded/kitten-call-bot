from types import SimpleNamespace

from kitten_bot import __main__


def test_missing_token_exits_with_actionable_message(monkeypatch, capsys):
    monkeypatch.delenv("BOT_TOKEN", raising=False)
    assert __main__.main() == 1
    assert "BOT_TOKEN" in capsys.readouterr().err


def test_dependency_error_never_prints_token(monkeypatch, tmp_path, capsys):
    token = "123456789:FAKE_TEST_SECRET_DO_NOT_LOG"
    monkeypatch.setenv("BOT_TOKEN", token)
    monkeypatch.setenv("DB_PATH", str(tmp_path / "db.sqlite3"))

    def fail(**kwargs):
        raise RuntimeError(f"Failed https://api.telegram.org/bot{token}/getUpdates")

    monkeypatch.setattr(
        __main__, "build_application", lambda *args: SimpleNamespace(run_polling=fail)
    )
    assert __main__.main() == 1
    output = capsys.readouterr()
    assert token not in output.out + output.err
    assert "RuntimeError" in output.err


def test_graceful_shutdown_keeps_persistent_database(monkeypatch, tmp_path):
    monkeypatch.setenv("BOT_TOKEN", "123456789:FAKE_TEST_SECRET")
    monkeypatch.setenv("DB_PATH", str(tmp_path / "db.sqlite3"))

    def build(config, store, photos):
        assert len(photos.files) >= 2
        assert store.settings(-1).window == 40
        return SimpleNamespace(run_polling=lambda **kwargs: None)

    monkeypatch.setattr(__main__, "build_application", build)
    assert __main__.main() == 0
    assert (tmp_path / "db.sqlite3").is_file()
