from __future__ import annotations

import tempfile
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


def main() -> None:
    import app.database as database
    import app.llm_client as llm

    original_db_path = database.DB_PATH
    original_load_config = llm.load_config
    original_get_env = llm._get_env
    original_post = llm.requests.post
    try:
        with tempfile.TemporaryDirectory() as tmp:
            database.DB_PATH = Path(tmp) / "llm_config.db"
            database.init_db()

            captured: dict[str, str] = {}

            class FakeResponse:
                def raise_for_status(self) -> None:
                    return None

                def json(self) -> dict:
                    return {"choices": [{"message": {"content": "ok"}}]}

            def fake_post(url, **kwargs):
                captured["url"] = str(url)
                return FakeResponse()

            llm._get_env = lambda name: "test-key" if name == "MOONSHOT_API_KEY" else None
            llm.requests.post = fake_post
            llm.load_config = lambda: {
                "llm": {
                    "provider": "kimi",
                    "model": "moonshot-v1-auto",
                    "api_key_env": "MOONSHOT_API_KEY",
                },
                "llm_routes": {},
            }
            assert llm.call_llm_api([{"role": "user", "content": "hello"}], task="chat") == "ok"
            assert captured["url"] == "https://api.moonshot.cn/v1/chat/completions"

            llm._get_env = original_get_env
            llm.requests.post = original_post
            llm.load_config = lambda: {
                "llm": {
                    "provider": "kimi",
                    "api_key_env": "MNEMOSYNE_TEST_KEY_THAT_DOES_NOT_EXIST",
                },
                "llm_routes": {},
            }
            try:
                llm.call_llm_api([{"role": "user", "content": "hello"}], task="chat")
            except llm.LLMProviderError as exc:
                assert "MNEMOSYNE_TEST_KEY_THAT_DOES_NOT_EXIST" in str(exc)
            else:
                raise AssertionError("missing api key did not raise LLMProviderError")

            llm.load_config = lambda: {"llm": {"provider": "not-a-provider"}, "llm_routes": {}}
            try:
                llm.call_llm_api([{"role": "user", "content": "hello"}], task="chat")
            except llm.LLMProviderError:
                pass
            else:
                raise AssertionError("unsupported provider did not raise LLMProviderError")

            from app.server import _safe_llm_config
            import app.server as server
            original_server_load_config = server.load_config
            server.load_config = lambda: {
                "llm": {
                    "provider": "kimi",
                    "model": "moonshot-v1-auto",
                    "api_key_env": "MOONSHOT_API_KEY",
                },
                "llm_routes": {},
            }

            safe = _safe_llm_config({
                "provider": "kimi",
                "model": "moonshot-v1-auto",
                "base_url": "https://api.moonshot.cn/v1",
                "api_key_env": "MNEMOSYNE_TEST_KEY_THAT_DOES_NOT_EXIST",
                "max_tokens": 360,
                "timeout": 25,
            })
            assert safe["api_key_env_present"] is False
            assert safe["base_url"] == "https://api.moonshot.cn/v1"
            assert safe["max_tokens"] == 360
            assert safe["timeout"] == 25

            ts = database.now_ts()
            with database.get_db() as db:
                db.executemany(
                    """
                    INSERT INTO llm_call_logs (
                        task, provider, model, status, prompt_chars, response_chars,
                        duration_ms, error_text, created_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    [
                        ("health_probe", "kimi", "moonshot-v1-auto", "success", 100, 50, 1200, "", ts),
                        ("health_probe", "kimi", "moonshot-v1-auto", "failed", 120, 0, 31000, "timeout", ts + 1),
                        ("group_chat", "kimi", "moonshot-v1-auto", "success", 80, 40, 900, "", ts + 2),
                        ("recovered_probe", "kimi", "moonshot-v1-auto", "failed", 80, 0, 1200, "old timeout", ts + 3),
                        ("recovered_probe", "kimi", "moonshot-v1-auto", "success", 80, 40, 900, "", ts + 4),
                        ("stale_config_probe", "not-a-provider", "", "failed", 80, 0, 1200, "old route", ts + 5),
                        ("context_probe", "kimi", "moonshot-v1-auto", "success", 40000, 1000, 2000, "", ts + 6),
                    ],
                )
            health = server.admin_llm_health({"id": 1, "role": "admin"}, limit=10)
            probe_health = next(item for item in health["tasks"] if item["task"] == "health_probe")
            recovered_health = next(item for item in health["tasks"] if item["task"] == "recovered_probe")
            stale_health = next(item for item in health["tasks"] if item["task"] == "stale_config_probe")
            context_health = next(item for item in health["tasks"] if item["task"] == "context_probe")
            assert health["window"] >= 3
            assert health["failed"] >= 1
            assert health["slow"] >= 1
            assert probe_health["failed"] == 1
            assert probe_health["slow"] == 1
            assert probe_health["last_error"] == "timeout"
            assert probe_health["current_failed"] is True
            assert probe_health["prompt_chars_total"] == 220
            assert probe_health["response_chars_total"] == 50
            assert probe_health["estimated_prompt_tokens"] == 55
            assert probe_health["estimated_response_tokens"] == 12
            assert probe_health["estimated_total_tokens"] == 67
            assert probe_health["avg_prompt_chars"] == 110
            assert probe_health["avg_response_chars"] == 25
            assert recovered_health["failed"] == 1
            assert recovered_health["current_failed"] is False
            assert recovered_health["historical_failed"] is True
            assert stale_health["failed"] == 1
            assert stale_health["current_failed"] is False
            assert stale_health["historical_failed"] is True
            assert stale_health["stale_config_failure"] is True
            assert context_health["cost_pressure"] == "high_context"
            assert context_health["route_hint"] == "review_context_size"
            assert context_health["estimated_total_tokens"] == 10250
    finally:
        if "server" in locals() and "original_server_load_config" in locals():
            server.load_config = original_server_load_config
        llm.load_config = original_load_config
        llm._get_env = original_get_env
        llm.requests.post = original_post
        database.DB_PATH = original_db_path

    print("LLM config verification passed")


if __name__ == "__main__":
    main()
