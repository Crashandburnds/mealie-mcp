from pydantic import ValidationError
from starlette.testclient import TestClient

from mealie_mcp.__main__ import config_error_app, config_problems
from mealie_mcp.config import Settings


def test_bad_config_is_reported_without_values(monkeypatch):
    for key in ("MEALIE_URL", "MEALIE_API_TOKEN", "OWNER_PASSWORD", "AUTH_SECRET"):
        monkeypatch.delenv(key, raising=False)
    try:
        Settings(_env_file=None, mealie_url="https://m.example.com", mealie_api_token="tok", owner_password="short-pw1")
    except ValidationError as exc:
        problems = config_problems(exc)
    else:
        raise AssertionError("expected a validation error")
    assert any("OWNER_PASSWORD" in p["problem"] for p in problems)
    with TestClient(config_error_app(problems)) as client:
        r = client.get("/health")
    assert r.status_code == 503
    assert r.json()["status"] == "config_error"
    assert "short-pw1" not in r.text and "tok" not in r.text.replace("token", "")
