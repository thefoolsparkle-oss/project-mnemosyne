from __future__ import annotations

import json
import sys
import tempfile
import warnings
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
            db.execute(
                """
                INSERT INTO persona_versions (
                    persona_id, version, name, summary, prompt, traits_json,
                    relationship, speaking_style, boundaries_json,
                    psychological_profile_json, psychological_fit_notes,
                    appearance_description, desired_image, growth_notes,
                    reason, change_type, change_notes_json, created_at
                )
                VALUES (?, 1, 'Smoke', 'quiet smoke persona', 'chat naturally', '[]',
                        'tester', 'short', '[]', '{}', '', '', 'blue gray local avatar', '',
                        'initial smoke', 'initial_forge', '[]', ?)
                """,
                (persona_id, ts),
            )
            db.execute(
                """
                INSERT INTO persona_versions (
                    persona_id, version, name, summary, prompt, traits_json,
                    relationship, speaking_style, boundaries_json,
                    psychological_profile_json, psychological_fit_notes,
                    appearance_description, desired_image, growth_notes,
                    reason, change_type, change_notes_json, created_at
                )
                VALUES (?, 2, 'Smoke', 'changed smoke persona', 'chat naturally', '[]',
                        'tester', 'longer', '[]', '{}', '', '', 'blue gray local avatar', '',
                        'changed smoke', 'user_profile_update', '[]', ?)
                """,
                (persona_id, ts),
            )
            db.execute(
                "UPDATE personas SET summary = 'changed smoke persona', speaking_style = 'longer', version = 2 WHERE id = ?",
                (persona_id,),
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
        restored = post_json(
            opener,
            base_url,
            f"/api/personas/{persona_id}/versions/1/restore",
            {"note": "smoke restore"},
        )
        if restored.get("version") != 3 or restored.get("persona", {}).get("summary") != "quiet smoke persona":
            raise AssertionError("persona version restore route did not restore v1")
    finally:
        with database.get_db() as db:
            db.execute("DELETE FROM users WHERE id = ?", (user_id,))


def verify_avatar_route_in_process(client) -> None:
    guest_response = client.post("/api/auth/guest", json={})
    assert guest_response.status_code == 200
    guest = guest_response.json()
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
            db.execute(
                """
                INSERT INTO persona_versions (
                    persona_id, version, name, summary, prompt, traits_json,
                    relationship, speaking_style, boundaries_json,
                    psychological_profile_json, psychological_fit_notes,
                    appearance_description, desired_image, growth_notes,
                    reason, change_type, change_notes_json, created_at
                )
                VALUES (?, 1, 'Smoke', 'quiet smoke persona', 'chat naturally', '[]',
                        'tester', 'short', '[]', '{}', '', '', 'blue gray local avatar', '',
                        'initial smoke', 'initial_forge', '[]', ?)
                """,
                (persona_id, ts),
            )
            db.execute(
                """
                INSERT INTO persona_versions (
                    persona_id, version, name, summary, prompt, traits_json,
                    relationship, speaking_style, boundaries_json,
                    psychological_profile_json, psychological_fit_notes,
                    appearance_description, desired_image, growth_notes,
                    reason, change_type, change_notes_json, created_at
                )
                VALUES (?, 2, 'Smoke', 'changed smoke persona', 'chat naturally', '[]',
                        'tester', 'longer', '[]', '{}', '', '', 'blue gray local avatar', '',
                        'changed smoke', 'user_profile_update', '[]', ?)
                """,
                (persona_id, ts),
            )
            db.execute(
                "UPDATE personas SET summary = 'changed smoke persona', speaking_style = 'longer', version = 2 WHERE id = ?",
                (persona_id,),
            )
        generated_response = client.post(
            f"/api/personas/{persona_id}/avatar/generate",
            json={"desired_image": "blue gray local avatar"},
        )
        assert generated_response.status_code == 200
        generated = generated_response.json()
        if not generated.get("ok") or not str(generated.get("url") or "").endswith(".svg"):
            raise AssertionError("avatar generation route did not return an svg url")
        assert client.get(generated["url"]).status_code == 200
        restored_response = client.post(
            f"/api/personas/{persona_id}/versions/1/restore",
            json={"note": "smoke restore"},
        )
        assert restored_response.status_code == 200
        restored = restored_response.json()
        if restored.get("version") != 3 or restored.get("persona", {}).get("summary") != "quiet smoke persona":
            raise AssertionError("persona version restore route did not restore v1")
    finally:
        with database.get_db() as db:
            db.execute("DELETE FROM users WHERE id = ?", (user_id,))


def verify_in_process() -> None:
    warnings.filterwarnings("ignore", message=r"Using `httpx` with `starlette\.testclient` is deprecated.*")
    from fastapi.testclient import TestClient

    with tempfile.TemporaryDirectory() as tmp:
        database.DB_PATH = Path(tmp) / "http-smoke.db"
        import app.server as server

        server.UPLOAD_DIR = Path(tmp) / "uploads"
        server.UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
        for route in server.app.routes:
            if getattr(route, "path", "") == "/uploads" and hasattr(route, "app"):
                route.app.directory = str(server.UPLOAD_DIR)
                route.app.all_directories = [str(server.UPLOAD_DIR)]
                route.app.config_checked = False
        database.init_db()
        client = TestClient(server.app)
        for path, expected in {
            "/": 200,
            "/admin": 200,
            "/api/persona-options": 200,
            "/api/health": 200,
        }.items():
            response = client.get(path)
            if response.status_code != expected:
                raise AssertionError(f"{path}: expected {expected}, got {response.status_code}")
        verify_avatar_route_in_process(client)
    print("HTTP smoke verification passed in process")


def main() -> None:
    if len(sys.argv) > 1 and sys.argv[1] == "--in-process":
        verify_in_process()
        return
    base_url = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8000"
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
