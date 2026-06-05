from __future__ import annotations

import sys
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


def fetch_status(base_url: str, path: str) -> int:
    request = Request(f"{base_url.rstrip('/')}{path}", headers={"User-Agent": "mnemosyne-smoke/1.0"})
    with urlopen(request, timeout=5) as response:
        return int(response.status)


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
    if failures:
        raise AssertionError("; ".join(failures))
    print(f"HTTP smoke verification passed for {base_url.rstrip('/')}")


if __name__ == "__main__":
    main()
