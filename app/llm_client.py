from __future__ import annotations

import os
import sys
import time
from typing import Any
from typing import Dict, List

import requests

from .config import load_config
from .database import get_db, now_ts


Message = Dict[str, str]


class LLMProviderError(RuntimeError):
    def __init__(self, message: str, *, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code
        self.user_message = "服务暂时繁忙，请稍后再试。"


def messages_to_prompt(messages: List[Message]) -> str:
    parts: list[str] = []
    for msg in messages:
        role = msg.get("role", "user").upper()
        content = msg.get("content", "")
        parts.append(f"[{role}]\n{content}")
    parts.append("[ASSISTANT]\n")
    return "\n\n".join(parts)


def _get_env(name: str) -> str | None:
    value = os.getenv(name)
    if value:
        return value
    if sys.platform != "win32":
        return None
    try:
        import winreg

        for root, path in (
            (winreg.HKEY_CURRENT_USER, "Environment"),
            (winreg.HKEY_LOCAL_MACHINE, r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment"),
        ):
            try:
                with winreg.OpenKey(root, path) as key:
                    value, _ = winreg.QueryValueEx(key, name)
                    if value:
                        return str(value)
            except OSError:
                continue
    except Exception:
        return None
    return None


def api_key_env_present(name: str) -> bool:
    return bool(_get_env(str(name or "").strip()))


def _call_ollama(messages: List[Message], llm_config: dict) -> str:
    base_url = str(llm_config.get("base_url") or "http://localhost:11434").rstrip("/")
    payload = {
        "model": llm_config.get("model") or "qwen2.5:3b",
        "prompt": messages_to_prompt(messages),
        "stream": False,
        "options": {
            "temperature": float(llm_config.get("temperature", 0.75)),
        },
    }

    try:
        response = requests.post(f"{base_url}/api/generate", json=payload, timeout=120)
        response.raise_for_status()
    except requests.RequestException as exc:
        status_code = getattr(getattr(exc, "response", None), "status_code", None)
        raise LLMProviderError(f"ollama request failed with status {status_code or 'unknown'}", status_code=status_code) from exc
    data = response.json()
    return str(data.get("response", "")).strip()


def _call_chat_completions(
    messages: List[Message],
    llm_config: dict,
    *,
    provider_name: str,
    env_key: str,
    default_base_url: str,
    default_model: str,
    include_temperature: bool = True,
) -> str:
    configured_env_key = str(llm_config.get("api_key_env") or env_key)
    api_key = _get_env(configured_env_key)
    if not api_key:
        raise LLMProviderError(f"{configured_env_key} is not set, but config.yaml selects provider: {provider_name}")

    configured_base_url = str(llm_config.get("base_url") or "").rstrip("/")
    if not configured_base_url or configured_base_url == "http://localhost:11434":
        base_url = default_base_url.rstrip("/")
    else:
        base_url = configured_base_url
    payload = {
        "model": llm_config.get("model") or default_model,
        "messages": messages,
    }
    if include_temperature:
        payload["temperature"] = float(llm_config.get("temperature", 0.75))
    if llm_config.get("max_tokens"):
        payload["max_tokens"] = int(llm_config.get("max_tokens") or 0)
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    try:
        response = requests.post(
            f"{base_url}/chat/completions",
            json=payload,
            headers=headers,
            timeout=float(llm_config.get("timeout", 60)),
        )
        response.raise_for_status()
    except requests.RequestException as exc:
        status_code = getattr(getattr(exc, "response", None), "status_code", None)
        raise LLMProviderError(f"{provider_name} request failed with status {status_code or 'unknown'}", status_code=status_code) from exc
    data = response.json()
    return str(data["choices"][0]["message"]["content"]).strip()


def _call_openai(messages: List[Message], llm_config: dict) -> str:
    return _call_chat_completions(
        messages,
        llm_config,
        provider_name="openai",
        env_key="OPENAI_API_KEY",
        default_base_url="https://api.openai.com/v1",
        default_model="gpt-4o-mini",
    )


def _call_deepseek(messages: List[Message], llm_config: dict) -> str:
    return _call_chat_completions(
        messages,
        llm_config,
        provider_name="deepseek",
        env_key="DEEPSEEK_API_KEY",
        default_base_url="https://api.deepseek.com/v1",
        default_model="deepseek-v4-flash",
    )


def _call_kimi(messages: List[Message], llm_config: dict) -> str:
    return _call_chat_completions(
        messages,
        llm_config,
        provider_name="kimi",
        env_key="MOONSHOT_API_KEY",
        default_base_url="https://api.moonshot.cn/v1",
        default_model="kimi-k2.6",
        include_temperature=False,
    )


def call_llm_api(messages: List[Message], task: str = "default") -> str:
    llm_config = _llm_config_for_task(task)
    provider = str(llm_config.get("provider", "ollama")).lower()
    model = str(llm_config.get("model") or "")
    started = time.perf_counter()
    prompt_chars = sum(len(str(message.get("content", ""))) for message in messages)

    try:
        if provider == "ollama":
            response = _call_ollama(messages, llm_config)
        elif provider == "openai":
            response = _call_openai(messages, llm_config)
        elif provider == "deepseek":
            response = _call_deepseek(messages, llm_config)
        elif provider in {"kimi", "moonshot"}:
            response = _call_kimi(messages, llm_config)
        elif provider in {"openai_compatible", "compatible"} or llm_config.get("api_key_env"):
            response = _call_compatible_provider(messages, llm_config)
        else:
            raise LLMProviderError(f"Unsupported LLM provider: {provider}")
    except Exception as exc:
        _record_llm_call(
            task=task,
            provider=provider,
            model=model,
            status="failed",
            prompt_chars=prompt_chars,
            response_chars=0,
            duration_ms=_elapsed_ms(started),
            error_text=f"{type(exc).__name__}: {exc}",
        )
        raise

    _record_llm_call(
        task=task,
        provider=provider,
        model=model,
        status="success",
        prompt_chars=prompt_chars,
        response_chars=len(response),
        duration_ms=_elapsed_ms(started),
    )
    return response


def _llm_config_for_task(task: str) -> dict[str, Any]:
    config = load_config()
    base = dict(config.get("llm", {}) or {})
    routes = config.get("llm_routes", {}) or {}
    route = routes.get(task) or routes.get("default") or {}
    if isinstance(route, dict):
        base.update(route)
    return base


def _call_compatible_provider(messages: List[Message], llm_config: dict) -> str:
    provider_name = str(llm_config.get("provider_name") or llm_config.get("provider") or "openai_compatible")
    api_key_env = str(llm_config.get("api_key_env") or "")
    if not api_key_env:
        raise LLMProviderError("api_key_env is required for openai-compatible LLM providers")
    return _call_chat_completions(
        messages,
        llm_config,
        provider_name=provider_name,
        env_key=api_key_env,
        default_base_url=str(llm_config.get("base_url") or ""),
        default_model=str(llm_config.get("model") or ""),
    )


def _record_llm_call(
    *,
    task: str,
    provider: str,
    model: str,
    status: str,
    prompt_chars: int,
    response_chars: int,
    duration_ms: int,
    error_text: str = "",
) -> None:
    try:
        with get_db() as db:
            db.execute(
                """
                CREATE TABLE IF NOT EXISTS llm_call_logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    task TEXT NOT NULL DEFAULT 'default',
                    provider TEXT NOT NULL DEFAULT '',
                    model TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'pending',
                    prompt_chars INTEGER NOT NULL DEFAULT 0,
                    response_chars INTEGER NOT NULL DEFAULT 0,
                    duration_ms INTEGER NOT NULL DEFAULT 0,
                    error_text TEXT NOT NULL DEFAULT '',
                    created_at INTEGER NOT NULL
                )
                """
            )
            db.execute(
                """
                INSERT INTO llm_call_logs (
                    task, provider, model, status, prompt_chars, response_chars,
                    duration_ms, error_text, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    task or "default",
                    provider,
                    model,
                    status,
                    int(prompt_chars),
                    int(response_chars),
                    int(duration_ms),
                    error_text[:2000],
                    now_ts(),
                ),
            )
    except Exception as exc:
        print("[LLMLog] write failed:", exc)


def _elapsed_ms(started: float) -> int:
    return int((time.perf_counter() - started) * 1000)
