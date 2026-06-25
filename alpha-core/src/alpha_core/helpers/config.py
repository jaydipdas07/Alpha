"""Config loading — the single source of truth for tunables (CLAUDE.md).

No magic numbers in code: every business parameter lives in ``config/``, loaded here
and validated by a pydantic model at the call site. The config dir is the repo-root
``config/``, overridable via ``ALPHA_CONFIG_DIR`` (tests / deployment). The full
layered loader (per-env configs, the live-mode gate, secret scanning) is lifted from
Vega in B0.9.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml


class ConfigError(Exception):
    """Raised when configuration is missing or invalid (fail fast)."""


def _config_dir() -> Path:
    """Resolve the active config directory (``ALPHA_CONFIG_DIR`` or the repo root)."""
    override = os.environ.get("ALPHA_CONFIG_DIR")
    if override:
        return Path(override)
    for parent in Path(__file__).resolve().parents:
        candidate = parent / "config"
        if candidate.is_dir():
            return candidate
    raise ConfigError(  # pragma: no cover - defensive; config/ sits at the repo root
        "could not locate the config/ directory; set ALPHA_CONFIG_DIR"
    )


def load_yaml(name: str) -> dict[str, Any]:
    """Load and parse ``config/<name>`` as a mapping (honors ``ALPHA_CONFIG_DIR``)."""
    path = _config_dir() / name
    if not path.is_file():
        raise ConfigError(f"missing config file: {path}")
    raw = yaml.safe_load(path.read_text()) or {}
    if not isinstance(raw, dict):
        raise ConfigError(f"config {name} must be a mapping at top level")
    return raw
