from __future__ import annotations

import os
import stat
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from pydantic.fields import FieldInfo
from pydantic_settings import BaseSettings, PydanticBaseSettingsSource, SettingsError

PRODUCTION_ENVIRONMENTS = frozenset({"prod", "production", "release"})
MAX_SECRET_FILE_BYTES = 64 * 1024


def is_production_environment(value: str) -> bool:
    return value.strip().lower() in PRODUCTION_ENVIRONMENTS


class SecretFileLoadError(ValueError):
    """A deliberately sanitized secret-file loading error."""


def load_secret_file(path_value: str, *, setting_name: str, production: bool) -> str:
    """Load one bounded UTF-8 value without disclosing its path or contents on failure."""

    display_name = setting_name.upper()
    path = Path(path_value)
    if not path_value.strip():
        raise SecretFileLoadError(f"{display_name}_FILE must not be empty")
    if production and not path.is_absolute():
        raise SecretFileLoadError(
            f"{display_name}_FILE must reference an absolute path in prod/release"
        )

    try:
        path_metadata = path.stat()
        if not stat.S_ISREG(path_metadata.st_mode):
            raise SecretFileLoadError(f"{display_name}_FILE must reference a regular file")
        if path_metadata.st_size > MAX_SECRET_FILE_BYTES:
            raise SecretFileLoadError(
                f"{display_name}_FILE exceeds the maximum size of {MAX_SECRET_FILE_BYTES} bytes"
            )
        with path.open("rb") as handle:
            metadata = os.fstat(handle.fileno())
            if not stat.S_ISREG(metadata.st_mode):
                raise SecretFileLoadError(f"{display_name}_FILE must reference a regular file")
            if metadata.st_size > MAX_SECRET_FILE_BYTES:
                raise SecretFileLoadError(
                    f"{display_name}_FILE exceeds the maximum size of {MAX_SECRET_FILE_BYTES} bytes"
                )
            contents = handle.read(MAX_SECRET_FILE_BYTES + 1)
    except SecretFileLoadError:
        raise
    except (OSError, ValueError):
        raise SecretFileLoadError(f"{display_name}_FILE could not be read safely") from None

    if len(contents) > MAX_SECRET_FILE_BYTES:
        raise SecretFileLoadError(
            f"{display_name}_FILE exceeds the maximum size of {MAX_SECRET_FILE_BYTES} bytes"
        )
    try:
        value = contents.decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        raise SecretFileLoadError(f"{display_name}_FILE must contain valid UTF-8") from None
    if "\x00" in value:
        raise SecretFileLoadError(f"{display_name}_FILE must not contain NUL bytes")
    if value.endswith("\r\n"):
        value = value[:-2]
    elif value.endswith("\n"):
        value = value[:-1]
    if not value.strip():
        raise SecretFileLoadError(f"{display_name}_FILE must contain a non-empty value")
    return value


class SecretFileSettingsSource(PydanticBaseSettingsSource):
    """Resolve `<SETTING>_FILE` after normal inline settings sources."""

    def __init__(
        self,
        settings_cls: type[BaseSettings],
        *,
        dotenv_values: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(settings_cls)
        self._environment = _casefold_mapping(os.environ)
        self._dotenv_values = _casefold_mapping(dotenv_values or {})

    def get_field_value(
        self,
        field: FieldInfo,
        field_name: str,
    ) -> tuple[Any, str, bool]:
        del field
        reference = self._file_reference(field_name)
        return reference, field_name, False

    def __call__(self) -> dict[str, Any]:
        references = {
            field_name: reference
            for field_name in self.settings_cls.model_fields
            if (reference := self._file_reference(field_name)) is not None
        }
        for field_name in references:
            if field_name in self.current_state:
                display_name = field_name.upper()
                raise SettingsError(
                    f"{display_name} cannot be set together with {display_name}_FILE"
                )

        app_env = str(self.current_state.get("app_env", "local"))
        if "app_env" in references:
            try:
                app_env = load_secret_file(
                    references["app_env"],
                    setting_name="app_env",
                    production=False,
                )
            except SecretFileLoadError as exc:
                raise SettingsError(str(exc)) from None
        production = is_production_environment(app_env)

        loaded: dict[str, Any] = {}
        for field_name, reference in references.items():
            try:
                loaded[field_name] = load_secret_file(
                    reference,
                    setting_name=field_name,
                    production=production,
                )
            except SecretFileLoadError as exc:
                raise SettingsError(str(exc)) from None
        return loaded

    def _file_reference(self, field_name: str) -> str | None:
        environment_name = f"{field_name}_file".casefold()
        raw_value = self._environment.get(environment_name)
        if raw_value is None:
            raw_value = self._dotenv_values.get(environment_name)
        if raw_value is None:
            return None
        return str(raw_value)


def _casefold_mapping(values: Mapping[str, Any]) -> dict[str, Any]:
    return {str(key).casefold(): value for key, value in values.items()}
