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
    try:
        with tempfile.TemporaryDirectory() as tmp:
            database.DB_PATH = Path(tmp) / "llm_config.db"
            database.init_db()

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
    finally:
        llm.load_config = original_load_config
        database.DB_PATH = original_db_path

    print("LLM config verification passed")


if __name__ == "__main__":
    main()
