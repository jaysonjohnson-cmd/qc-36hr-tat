import os
import datetime
import pathlib
import pytest
import jwt

os.environ["LOCAL_DEV"] = "1"
# No JWT_SIGNING_SECRET set — simulates local dev

from main import app


@pytest.fixture
def client():
    app.config["TESTING"] = True
    with app.test_client() as client:
        yield client


def _make_dev_token(email="dev@storesight.com", name="Dev User", expired=False):
    now = datetime.datetime.now(datetime.timezone.utc)
    exp = now - datetime.timedelta(hours=1) if expired else now + datetime.timedelta(hours=8)
    payload = {
        "email": email,
        "name": name,
        "aud": "storesight-dev",
        "iat": now,
        "exp": exp,
    }
    return jwt.encode(payload, "irrelevant-secret", algorithm="HS256")


class TestLocalDevAuth:
    @pytest.fixture(autouse=True)
    def setup_token_dir(self, tmp_path, monkeypatch):
        self.token_dir = tmp_path / ".storesight"
        self.token_dir.mkdir()
        self.token_file = self.token_dir / "dev-token"
        monkeypatch.setattr(
            "main._dev_token_path",
            lambda: self.token_file,
        )

    def test_health_always_works(self, client):
        resp = client.get("/health")
        assert resp.status_code == 200

    def test_no_token_file_returns_error_page(self, client):
        resp = client.get("/")
        assert resp.status_code == 401
        assert b"dev token" in resp.data.lower() or b"Dev token" in resp.data

    def test_expired_token_returns_error_page(self, client):
        self.token_file.write_text(_make_dev_token(expired=True))
        resp = client.get("/")
        assert resp.status_code == 401
        assert b"expired" in resp.data.lower()

    def test_valid_token_populates_g_user(self, client):
        self.token_file.write_text(_make_dev_token(email="kelly@storesight.com", name="Kelly"))
        resp = client.get("/")
        assert resp.status_code == 200

    def test_logout_still_works(self, client):
        self.token_file.write_text(_make_dev_token())
        resp = client.get("/logout")
        assert resp.status_code == 302
