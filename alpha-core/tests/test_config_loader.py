"""Config loader: directory resolution + mapping validation (fail-fast)."""

from __future__ import annotations

from pathlib import Path

import pytest

from alpha_core.helpers.config import ConfigError, load_yaml


def test_missing_file_raises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ALPHA_CONFIG_DIR", str(tmp_path))
    with pytest.raises(ConfigError, match="missing config file"):
        load_yaml("nope.yaml")


def test_non_mapping_raises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ALPHA_CONFIG_DIR", str(tmp_path))
    (tmp_path / "list.yaml").write_text("- a\n- b\n")
    with pytest.raises(ConfigError, match="must be a mapping"):
        load_yaml("list.yaml")


def test_loads_mapping_with_override(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ALPHA_CONFIG_DIR", str(tmp_path))
    (tmp_path / "x.yaml").write_text("a: 1\nb: two\n")
    assert load_yaml("x.yaml") == {"a": 1, "b": "two"}


def test_empty_file_is_empty_mapping(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ALPHA_CONFIG_DIR", str(tmp_path))
    (tmp_path / "empty.yaml").write_text("")
    assert load_yaml("empty.yaml") == {}
