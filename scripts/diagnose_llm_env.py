from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.config import CONFIG_FILE, load_config


def _merged_routes(config: dict[str, Any]) -> dict[str, dict[str, Any]]:
    base = dict(config.get("llm", {}) or {})
    routes = config.get("llm_routes", {}) or {}
    merged: dict[str, dict[str, Any]] = {"default": base}
    if isinstance(routes, dict):
        for task, route in sorted(routes.items()):
            if not isinstance(route, dict):
                continue
            effective = dict(base)
            effective.update(route)
            merged[str(task)] = effective
    return merged


def _safe_row(task: str, config: dict[str, Any]) -> dict[str, Any]:
    env_name = str(config.get("api_key_env") or "").strip()
    return {
        "task": task,
        "provider": str(config.get("provider") or ""),
        "model": str(config.get("model") or ""),
        "base_url": str(config.get("base_url") or ""),
        "api_key_env": env_name,
        "api_key_env_present": bool(env_name and os.getenv(env_name)),
        "timeout": config.get("timeout", ""),
        "max_tokens": config.get("max_tokens", ""),
    }


def main() -> None:
    config = load_config()
    print(f"Config: {CONFIG_FILE}")
    for task, route in _merged_routes(config).items():
        row = _safe_row(task, route)
        status = "ready" if row["api_key_env_present"] else "missing"
        print(
            "{task}: provider={provider} model={model} base_url={base_url} "
            "api_key_env={api_key_env} env={status} timeout={timeout} max_tokens={max_tokens}".format(
                **row,
                status=status,
            )
        )


if __name__ == "__main__":
    main()
