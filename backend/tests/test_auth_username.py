from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from types import ModuleType

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import config as app_config


@pytest.fixture(autouse=True)
def isolated_auth(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[ModuleType]:
    monkeypatch.setattr(app_config.settings, "data_dir", tmp_path)
    from app.api import auth as auth_api
    from app.services import auth

    auth._sessions.clear()
    auth._configured_cache = None
    auth_api._fail_counter.clear()
    yield auth
    auth._sessions.clear()
    auth._configured_cache = None
    auth_api._fail_counter.clear()


@pytest.fixture
def client() -> TestClient:
    from app.api import auth as auth_api

    app = FastAPI()
    app.include_router(auth_api.router)
    # 127.0.0.1 = 本机, 通过 setup 的本机/内网闸门
    return TestClient(app, client=("127.0.0.1", 50000))


def _auth_json(tmp_path: Path) -> dict:
    return json.loads((tmp_path / "user_data" / "auth.json").read_text(encoding="utf-8"))


def test_legacy_auth_json_without_username_defaults_to_admin(isolated_auth: ModuleType, tmp_path: Path) -> None:
    auth = isolated_auth
    auth.set_password("legacy-secret")
    data = _auth_json(tmp_path)
    data.pop("username")
    (tmp_path / "user_data" / "auth.json").write_text(json.dumps(data), encoding="utf-8")

    assert auth.get_username() == "admin"
    assert auth.verify_and_create_session("legacy-secret", username="admin") is not None
    assert auth.verify_and_create_session("legacy-secret", username="root") is None
    # 只传密码的旧调用方仍可登录
    assert auth.verify_and_create_session("legacy-secret") is not None


def test_change_password_keeps_username(isolated_auth: ModuleType) -> None:
    auth = isolated_auth
    auth.set_password("first-secret", username="trader")
    auth.set_password("second-secret")

    assert auth.get_username() == "trader"
    assert auth.verify_and_create_session("second-secret", username="trader") is not None


def test_username_too_long_rejected(isolated_auth: ModuleType) -> None:
    with pytest.raises(ValueError):
        isolated_auth.set_password("secret123", username="x" * 33)


def test_setup_and_login_with_username(client: TestClient, tmp_path: Path) -> None:
    r = client.post("/api/auth/setup", json={"username": " admin ", "password": "Aa@333123"})
    assert r.status_code == 200
    stored = _auth_json(tmp_path)
    assert stored["username"] == "admin"
    assert "Aa@333123" not in json.dumps(stored)

    # 未登录时不暴露账号名
    assert "username" not in client.get("/api/auth/status").json()

    r = client.post("/api/auth/login", json={"username": "root", "password": "Aa@333123"})
    assert r.status_code == 401
    assert r.json()["detail"] == "用户名或密码错误"
    r = client.post("/api/auth/login", json={"username": "admin", "password": "wrong-pass"})
    assert r.status_code == 401
    assert r.json()["detail"] == "用户名或密码错误"

    r = client.post("/api/auth/login", json={"username": "admin", "password": "Aa@333123"})
    assert r.status_code == 200
    status = client.get("/api/auth/status").json()
    assert status == {"configured": True, "authenticated": True, "username": "admin"}


def test_setup_without_username_defaults_to_admin(client: TestClient, isolated_auth: ModuleType) -> None:
    assert client.post("/api/auth/setup", json={"password": "secret123"}).status_code == 200
    assert isolated_auth.get_username() == "admin"


def test_wrong_username_counts_toward_rate_limit(client: TestClient, isolated_auth: ModuleType) -> None:
    isolated_auth.set_password("secret123")
    for _ in range(5):
        assert client.post("/api/auth/login", json={"username": "nobody", "password": "secret123"}).status_code == 401
    r = client.post("/api/auth/login", json={"username": "admin", "password": "secret123"})
    assert r.status_code == 429
