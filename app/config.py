from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml


BASE_DIR = Path(__file__).resolve().parent.parent
CONFIG_FILE = BASE_DIR / "config.yaml"

DEFAULT_CONFIG: dict[str, Any] = {
    "active_persona": "",
    "llm": {
        "provider": "kimi",
        "model": "kimi-k2.6",
        "base_url": "https://api.moonshot.ai/v1",
        "api_key_env": "MOONSHOT_API_KEY",
        "temperature": 0.75,
    },
    "llm_routes": {},
    "memory": {
        "mode": "balanced",
        "short_term_turns": 8,
        "extract_every": 2,
        "consolidate_every": 10,
    },
}


def _deep_merge(defaults: dict[str, Any], overrides: dict[str, Any]) -> dict[str, Any]:
    result = dict(defaults)
    for key, value in overrides.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def load_config() -> dict[str, Any]:
    if not CONFIG_FILE.exists():
        return DEFAULT_CONFIG.copy()

    with CONFIG_FILE.open("r", encoding="utf-8") as f:
        loaded = yaml.safe_load(f) or {}

    if not isinstance(loaded, dict):
        raise ValueError("config.yaml must contain a YAML object")

    return _deep_merge(DEFAULT_CONFIG, loaded)
