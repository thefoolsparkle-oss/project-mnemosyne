from __future__ import annotations

import json
import sys
from http.cookiejar import CookieJar
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import HTTPCookieProcessor, Request, build_opener, urlopen

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import app.database as database


def fetch_status(base_url: str, path: str) -> int:
    request = Request(f"{base_url.rstrip('/')}{path}", headers={"User-Agent": "mnemosyne-smoke/1.0"})
    with urlopen(request, timeout=5) as response:
        return int(response.status)


def post_json(opener, base_url: str, path: str, payload: dict) -> dict:
    data = json.dumps(payload).encode("utf-8")
    request = Request(
        f"{base_url.rstrip('/')}{path}",
        data=data,
        headers={"Content-Type": "application/json", "User-Agent": "mnemosyne-smoke/1.0"},
        method="POST",
    )
    with opener.open(request, timeout=10) as response:
        return json.loads(response.read().decode("utf-8"))


def verify_avatar_route(base_url: str) -> None:
    opener = build_opener(HTTPCookieProcessor(CookieJar()))
    guest = post_json(opener, base_url, "/api/auth/guest", {})
    user_id = int(guest["user"]["id"])
    try:
        ts = database.now_ts()
        with database.get_db() as db:
            persona_id = int(
                db.execute(
                    """
                    INSERT INTO personas (
                        user_id, name, summary, prompt, relationship, speaking_style,
                        desired_image, created_at, updated_at
                    )
                    VALUES (?, 'Smoke', 'quiet smoke persona', 'chat naturally', 'tester', 'short',
                            'blue gray local avatar', ?, ?)
                    """,
                    (user_id, ts, ts),
                ).lastrowid
            )
        generated = post_json(
            opener,
            base_url,
            f"/api/personas/{persona_id}/avatar/generate",
            {"desired_image": "blue gray local avatar"},
        )
        if not generated.get("ok") or not str(generated.get("url") or "").endswith(".svg"):
            raise AssertionError("avatar generation route did not return an svg url")
        fetch_status(base_url, generated["url"])
    finally:
        with database.get_db() as db:
            db.execute("DELETE FROM users WHERE id = ?", (user_id,))


def main() -> None:
    base_url = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8001"
    checks = {
        "/": 200,
        "/admin": 200,
        "/api/persona-options": 200,
    }
    failures: list[str] = []
    for path, expected in checks.items():
        try:
            status = fetch_status(base_url, path)
        except HTTPError as exc:
            status = int(exc.code)
        except URLError as exc:
            failures.append(f"{path}: connection failed: {exc.reason}")
            continue
        except TimeoutError:
            failures.append(f"{path}: request timed out")
            continue
        if status != expected:
            failures.append(f"{path}: expected {expected}, got {status}")
    if not failures:
        try:
            verify_avatar_route(base_url)
        except Exception as exc:
            failures.append(f"avatar route: {exc}")
    if failures:
        raise AssertionError("; ".join(failures))
    print(f"HTTP smoke verification passed for {base_url.rstrip('/')}")


if __name__ == "__main__":
    main()
