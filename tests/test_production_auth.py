"""Tests for the production cookie-auth branch of require_auth in main.py.

The other auth test file (test_local_dev_auth.py) imports `main` with
LOCAL_DEV=1, so it only exercises the dev-token path. These tests flip the
module-level globals that `require_auth` reads (`LOCAL_DEV`, `JWT_SECRET`) to
drive the production `storesight_session` cookie path instead — covering the
missing-cookie redirect, a valid cookie, and rejected (expired / bad-signature)
cookies.
"""
import datetime

import jwt
import pytest

import main


@pytest.fixture
def client(monkeypatch):
    # Force the production branch regardless of how `main` was first imported.
    monkeypatch.setattr(main, "LOCAL_DEV", False)
    monkeypatch.setattr(main, "JWT_SECRET", "test-secret")
    main.app.config["TESTING"] = True
    with main.app.test_client() as c:
        yield c


def _session_token(secret="test-secret", email="rep@storesight.com", name="Rep", expired=False):
    now = datetime.datetime.now(datetime.timezone.utc)
    exp = now - datetime.timedelta(hours=1) if expired else now + datetime.timedelta(hours=8)
    return jwt.encode(
        {"email": email, "name": name, "iat": now, "exp": exp},
        secret,
        algorithm="HS256",
    )


class TestProductionCookieAuth:
    def test_missing_cookie_redirects_to_login(self, client):
        resp = client.get("/")
        assert resp.status_code == 302
        assert "auth-service.storesight.org/login" in resp.headers["Location"]

    def test_valid_cookie_allows_request(self, client):
        client.set_cookie("storesight_session", _session_token())
        resp = client.get("/")
        assert resp.status_code == 200
        assert b"Tool placeholder" in resp.data

    def test_expired_cookie_redirects_to_login(self, client):
        client.set_cookie("storesight_session", _session_token(expired=True))
        resp = client.get("/")
        assert resp.status_code == 302
        assert "/login" in resp.headers["Location"]

    def test_bad_signature_redirects_to_login(self, client):
        client.set_cookie("storesight_session", _session_token(secret="wrong-secret"))
        resp = client.get("/")
        assert resp.status_code == 302
        assert "/login" in resp.headers["Location"]

    def test_health_is_exempt_in_production(self, client):
        resp = client.get("/health")
        assert resp.status_code == 200
