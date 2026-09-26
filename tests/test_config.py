"""load_config precedence: explicit path > $LOL_COACH_CONFIG > PROJECT_ROOT/config.toml."""
from __future__ import annotations

import pytest

from lol_coach import config


def _write(path, marker):
    path.write_text(f'[paths]\nsqlite_db = "{marker}"\n', encoding="utf-8")
    return path


@pytest.fixture
def three_configs(tmp_path, monkeypatch):
    default = _write(tmp_path / "default.toml", "default")
    env = _write(tmp_path / "env.toml", "env")
    explicit = _write(tmp_path / "explicit.toml", "explicit")
    monkeypatch.setattr(config, "DEFAULT_CONFIG", default)
    monkeypatch.delenv(config.CONFIG_ENV_VAR, raising=False)
    return default, env, explicit


def _marker(cfg):
    return cfg["paths"]["sqlite_db"]


def test_default_file_when_nothing_else_is_given(three_configs):
    assert _marker(config.load_config()) == "default"


def test_env_var_overrides_the_default_file(three_configs, monkeypatch):
    _, env, _ = three_configs
    monkeypatch.setenv(config.CONFIG_ENV_VAR, str(env))
    assert _marker(config.load_config()) == "env"


def test_explicit_path_overrides_the_env_var(three_configs, monkeypatch):
    _, env, explicit = three_configs
    monkeypatch.setenv(config.CONFIG_ENV_VAR, str(env))
    assert _marker(config.load_config(explicit)) == "explicit"


def test_empty_env_var_is_treated_as_unset(three_configs, monkeypatch):
    monkeypatch.setenv(config.CONFIG_ENV_VAR, "")
    assert _marker(config.load_config()) == "default"


def test_missing_file_named_by_env_var_raises_the_usual_error(three_configs, monkeypatch, tmp_path):
    missing = tmp_path / "nope.toml"
    monkeypatch.setenv(config.CONFIG_ENV_VAR, str(missing))
    with pytest.raises(FileNotFoundError, match="config.toml not found at .*nope.toml"):
        config.load_config()
