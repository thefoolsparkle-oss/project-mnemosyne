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
                    ],
                )
            health = server.admin_llm_health({"id": 1, "role": "admin"}, limit=10)
            probe_health = next(item for item in health["tasks"] if item["task"] == "health_probe")
            assert health["window"] >= 3
            assert health["failed"] >= 1
            assert health["slow"] >= 1
            assert probe_health["failed"] == 1
            assert probe_health["slow"] == 1
            assert probe_health["last_error"] == "timeout"
    finally:
        llm.load_config = original_load_config
        llm._get_env = original_get_env
        llm.requests.post = original_post
        database.DB_PATH = original_db_path

    print("LLM config verification passed")


if __name__ == "__main__":
    main()
