from __future__ import annotations

from pathlib import Path

import pytest
from pydantic_settings import SettingsError

from app.core.config import Settings


def _clear_secret_environment(monkeypatch: pytest.MonkeyPatch, field_name: str) -> None:
    monkeypatch.delenv(field_name.upper(), raising=False)
    monkeypatch.delenv(f"{field_name.upper()}_FILE", raising=False)


def test_secret_file_loads_a_known_setting_and_removes_exactly_one_line_ending(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_secret_environment(monkeypatch, "auth_token_secret")
    secret_path = tmp_path / "auth-token"
    secret_path.write_bytes(b"  keep-surrounding-whitespace  \n\n")
    monkeypatch.setenv("AUTH_TOKEN_SECRET_FILE", str(secret_path))

    configured = Settings(_env_file=None)

    assert configured.auth_token_secret == "  keep-surrounding-whitespace  \n"


def test_secret_file_treats_crlf_as_one_terminal_line_ending(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_secret_environment(monkeypatch, "external_callback_secret")
    secret_path = tmp_path / "callback-secret"
    secret_path.write_bytes(b"callback-value\r\n")
    monkeypatch.setenv("EXTERNAL_CALLBACK_SECRET_FILE", str(secret_path))

    configured = Settings(_env_file=None)

    assert configured.external_callback_secret == "callback-value"


@pytest.mark.parametrize("inline_source", ["environment", "initializer"])
def test_inline_and_file_secret_sources_are_mutually_exclusive(
    inline_source: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    field_name = "auth_token_secret"
    _clear_secret_environment(monkeypatch, field_name)
    secret_path = tmp_path / "auth-token"
    file_canary = "file-only-sensitive-value"
    inline_canary = "inline-only-sensitive-value"
    secret_path.write_text(file_canary, encoding="utf-8")
    monkeypatch.setenv("AUTH_TOKEN_SECRET_FILE", str(secret_path))
    kwargs: dict[str, str] = {}
    if inline_source == "environment":
        monkeypatch.setenv("AUTH_TOKEN_SECRET", inline_canary)
    else:
        kwargs[field_name] = inline_canary

    with pytest.raises(SettingsError) as raised:
        Settings(_env_file=None, **kwargs)

    message = str(raised.value)
    assert "AUTH_TOKEN_SECRET" in message
    assert file_canary not in message
    assert inline_canary not in message
    assert str(secret_path) not in message


def test_file_reference_and_inline_value_conflict_inside_dotenv(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_secret_environment(monkeypatch, "auth_token_secret")
    secret_path = tmp_path / "auth-token"
    secret_path.write_text("file-sensitive-value", encoding="utf-8")
    dotenv_path = tmp_path / ".env"
    dotenv_path.write_text(
        f"AUTH_TOKEN_SECRET=inline-sensitive-value\nAUTH_TOKEN_SECRET_FILE={secret_path}\n",
        encoding="utf-8",
    )

    with pytest.raises(SettingsError) as raised:
        Settings(_env_file=dotenv_path)

    message = str(raised.value)
    assert "AUTH_TOKEN_SECRET" in message
    assert "inline-sensitive-value" not in message
    assert "file-sensitive-value" not in message
    assert str(secret_path) not in message


@pytest.mark.parametrize(
    ("contents", "reason"),
    [
        (b"", "empty"),
        (b"\n", "empty"),
        (b"secret\x00suffix", "NUL"),
        (b"\xff\xfe", "UTF-8"),
    ],
)
def test_secret_file_rejects_invalid_content_without_echoing_it(
    contents: bytes,
    reason: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_secret_environment(monkeypatch, "auth_token_secret")
    secret_path = tmp_path / "invalid-auth-token"
    secret_path.write_bytes(contents)
    monkeypatch.setenv("AUTH_TOKEN_SECRET_FILE", str(secret_path))

    with pytest.raises(SettingsError) as raised:
        Settings(_env_file=None)

    message = str(raised.value)
    assert reason in message
    assert str(secret_path) not in message
    assert "secret\x00suffix" not in message


def test_secret_file_rejects_non_regular_and_oversized_inputs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_secret_environment(monkeypatch, "auth_token_secret")
    monkeypatch.setenv("AUTH_TOKEN_SECRET_FILE", str(tmp_path))
    with pytest.raises(SettingsError, match="regular file"):
        Settings(_env_file=None)

    oversized_path = tmp_path / "oversized"
    oversized_path.write_bytes(b"x" * (64 * 1024 + 1))
    monkeypatch.setenv("AUTH_TOKEN_SECRET_FILE", str(oversized_path))
    with pytest.raises(SettingsError, match="maximum size"):
        Settings(_env_file=None)


def test_production_secret_file_path_must_be_absolute(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_secret_environment(monkeypatch, "auth_token_secret")
    relative_path = Path("relative-auth-token")
    (tmp_path / relative_path).write_text("never-disclosed", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("AUTH_TOKEN_SECRET_FILE", str(relative_path))

    with pytest.raises(SettingsError) as raised:
        Settings(_env_file=None)

    message = str(raised.value)
    assert "absolute" in message
    assert "never-disclosed" not in message
    assert str(relative_path) not in message


def test_production_rejects_inline_environment_secret_without_disclosing_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    canary = "must-never-appear-inline-production-secret"
    _clear_secret_environment(monkeypatch, "auth_token_secret")
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("AUTH_TOKEN_SECRET", canary)

    with pytest.raises(SettingsError) as raised:
        Settings(_env_file=None)

    message = str(raised.value)
    assert "AUTH_TOKEN_SECRET_FILE" in message
    assert canary not in message


def test_production_rejects_inline_initializer_secret_without_disclosing_it() -> None:
    canary = "must-never-appear-initializer-production-secret"

    with pytest.raises(SettingsError) as raised:
        Settings(
            _env_file=None,
            app_env="production",
            auth_token_secret=canary,
        )

    message = str(raised.value)
    assert "AUTH_TOKEN_SECRET_FILE" in message
    assert canary not in message


def test_production_rejects_inline_dotenv_secret_without_disclosing_it(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    canary = "must-never-appear-dotenv-production-secret"
    monkeypatch.delenv("APP_ENV", raising=False)
    _clear_secret_environment(monkeypatch, "audio_playback_grant_secret")
    dotenv_path = tmp_path / ".env"
    dotenv_path.write_text(
        f"APP_ENV=production\nAUDIO_PLAYBACK_GRANT_SECRET={canary}\n",
        encoding="utf-8",
    )

    with pytest.raises(SettingsError) as raised:
        Settings(_env_file=dotenv_path)

    message = str(raised.value)
    assert "AUDIO_PLAYBACK_GRANT_SECRET_FILE" in message
    assert canary not in message


def test_local_environment_keeps_inline_secret_compatibility(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    inline_value = "local-inline-secret-remains-supported"
    _clear_secret_environment(monkeypatch, "auth_token_secret")
    monkeypatch.setenv("AUTH_TOKEN_SECRET", inline_value)

    configured = Settings(_env_file=None, app_env="local")

    assert configured.auth_token_secret == inline_value
