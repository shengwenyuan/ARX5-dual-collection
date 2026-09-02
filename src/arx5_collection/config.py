from __future__ import annotations

import os
from pathlib import Path


CONFIG_ROOT_ENV = "ARX5_CONFIG_ROOT"
ENVIRONMENT_CONFIG_ENV = "ARX5_ENVIRONMENT_CONFIG"


def config_root() -> Path:
    configured = os.environ.get(CONFIG_ROOT_ENV)
    root = Path(configured) if configured is not None else Path.cwd() / "config"
    resolved = root.expanduser().resolve()
    if not resolved.is_dir():
        raise ValueError(f"configuration root does not exist: {resolved}")
    return resolved


def config_path(relative: str | Path) -> Path:
    root = config_root()
    path = (root / relative).resolve()
    path.relative_to(root)
    if not path.is_file():
        raise ValueError(f"configuration file does not exist: {path}")
    return path


def environment_config_path() -> Path:
    configured = os.environ.get(ENVIRONMENT_CONFIG_ENV)
    if configured is None:
        return config_path("environment/default.toml")
    path = Path(configured).expanduser().resolve()
    if not path.is_file():
        raise ValueError(f"environment configuration does not exist: {path}")
    return path
