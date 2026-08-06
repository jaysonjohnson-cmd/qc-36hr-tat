import os

import pytest

# Ensure a stable audience; OIDC is mocked in every test below.
os.environ.setdefault("INTERNAL_API_BASE", "http://localhost:8080")

import internal_api


@pytest.fixture(autouse=True)
def reset_state(monkeypatch):
    """Clear the module-level token cache between tests, never sleep during
    retry backoff, and default to no TOOL_SLUG unless a test sets one."""
    internal_api._token_cache["value"] = None
    internal_api._token_cache["exp"] = 0.0
    monkeypatch.setattr(internal_api.time, "sleep", lambda *_: None)
    monkeypatch.setattr(internal_api, "TOOL_SLUG", "")
    yield


def _patch_fetch(monkeypatch, side_effect):
    """Patch fetch_id_token. `side_effect` is a callable taking (request, audience)."""
    monkeypatch.setattr(
        internal_api.google.oauth2.id_token, "fetch_id_token", side_effect
    )


class TestProductionIdentityToken:
    @pytest.fixture(autouse=True)
    def force_prod(self, monkeypatch):
        monkeypatch.setattr(internal_api, "LOCAL_DEV", False)

    def test_returns_oidc_token(self, monkeypatch):
        _patch_fetch(monkeypatch, lambda *_: "oidc-tok")
        assert internal_api._get_headers() == {"Authorization": "Bearer oidc-tok"}

    def test_includes_tool_slug(self, monkeypatch):
        monkeypatch.setattr(internal_api, "TOOL_SLUG", "my-tool")
        _patch_fetch(monkeypatch, lambda *_: "oidc-tok")
        assert internal_api._get_headers() == {
            "X-Tool-Slug": "my-tool",
            "Authorization": "Bearer oidc-tok",
        }

    def test_caches_token_across_calls(self, monkeypatch):
        calls = {"n": 0}

        def fake(*_):
            calls["n"] += 1
            return "oidc-tok"

        _patch_fetch(monkeypatch, fake)
        internal_api._get_headers()
        internal_api._get_headers()
        assert calls["n"] == 1  # second call served from the cache

    def test_retries_then_succeeds(self, monkeypatch):
        seq = iter([RuntimeError("transient"), "oidc-tok"])

        def fake(*_):
            v = next(seq)
            if isinstance(v, Exception):
                raise v
            return v

        _patch_fetch(monkeypatch, fake)
        assert internal_api._get_headers() == {"Authorization": "Bearer oidc-tok"}

    def test_raises_when_token_unavailable(self, monkeypatch):
        def fake(*_):
            raise RuntimeError("metadata server down")

        _patch_fetch(monkeypatch, fake)
        with pytest.raises(RuntimeError):
            internal_api._get_headers()


class TestLocalDevToken:
    @pytest.fixture(autouse=True)
    def force_local(self, monkeypatch, tmp_path):
        monkeypatch.setattr(internal_api, "LOCAL_DEV", True)
        self.token_file = tmp_path / "dev-token"
        monkeypatch.setattr(internal_api, "_dev_token_path", lambda: self.token_file)

    def test_returns_dev_token_when_file_exists(self):
        self.token_file.write_text("dev-tok")
        assert internal_api._get_headers() == {"Authorization": "Bearer dev-tok"}

    def test_raises_when_no_file(self):
        with pytest.raises(RuntimeError):
            internal_api._get_headers()

    def test_raises_when_file_is_empty(self):
        self.token_file.write_text("")
        with pytest.raises(RuntimeError):
            internal_api._get_headers()
