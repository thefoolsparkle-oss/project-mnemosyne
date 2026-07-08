from __future__ import annotations

import sys
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.config import CONFIG_FILE, load_config
from app.database import get_db
from app.llm_client import api_key_env_present


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
        "api_key_env_present": api_key_env_present(env_name) if env_name else False,
        "timeout": config.get("timeout", ""),
        "max_tokens": config.get("max_tokens", ""),
    }


def _recent_llm_health(limit: int = 80) -> list[dict[str, Any]]:
    try:
        with get_db() as db:
            rows = db.execute(
                """
                SELECT task, status, duration_ms, error_text, created_at
                FROM llm_call_logs
                ORDER BY id DESC
                LIMIT ?
                """,
                (max(1, min(int(limit), 500)),),
            ).fetchall()
    except Exception:
        return []
    stats: dict[str, dict[str, Any]] = {}
    for row in rows:
        task = str(row["task"] or "default")
        item = stats.setdefault(
            task,
            {
                "task": task,
                "total": 0,
                "failed": 0,
                "slow": 0,
                "last_status": "",
                "last_error": "",
                "last_created_at": 0,
            },
        )
        item["total"] += 1
        status = str(row["status"] or "")
        created_at = int(row["created_at"] or 0)
        if status == "failed":
            item["failed"] += 1
            if not item["last_error"]:
                item["last_error"] = str(row["error_text"] or "")[:160]
        if int(row["duration_ms"] or 0) >= 30000:
            item["slow"] += 1
        if not item["last_created_at"] or created_at > int(item["last_created_at"] or 0):
            item["last_status"] = status
            item["last_created_at"] = created_at
            if status == "failed":
                item["last_error"] = str(row["error_text"] or "")[:160]
    for item in stats.values():
        item["current_failed"] = item.get("last_status") == "failed"
        item["historical_failed"] = int(item.get("failed") or 0) > 0 and not item["current_failed"]
    return sorted(
        stats.values(),
        key=lambda item: (
            0 if item.get("current_failed") else 1,
            -int(item["failed"]),
            -int(item["slow"]),
            str(item["task"]),
        ),
    )


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
    health = _recent_llm_health()
    if health:
        print("\nRecent local LLM health:")
        for item in health:
            line = "  {task}: total={total} failed={failed} slow={slow} last_status={last_status} last_at={last_created_at}".format(**item)
            if item.get("current_failed") and item.get("last_error"):
                line += f" current_error={item['last_error']}"
            elif item.get("historical_failed"):
                line += " historical_failed=true"
            print(line)
    else:
        print("\nRecent local LLM health: no local call logs")


if __name__ == "__main__":
    main()
