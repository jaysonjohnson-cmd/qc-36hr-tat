"""Tests for the per-tool Files helper (files.py + internal_api multipart/bytes).

These tests verify that the helper:
  1. Refuses to operate without TOOL_SLUG (would otherwise silently use the
     unscoped namespace and trip a 403 in production).
  2. Builds the right URL shape under /api/files/<slug>.
  3. Returns the inner `data` envelope to the caller without modification.

The `requests` library is mocked — no real network calls.
"""
import importlib
import os
from io import BytesIO
from unittest.mock import patch, MagicMock

import pytest


@pytest.fixture
def files_mod(monkeypatch):
    monkeypatch.setenv("TOOL_SLUG", "my-tool")
    import files as _files
    import internal_api as _api
    # Re-import to pick up the env var.
    importlib.reload(_api)
    importlib.reload(_files)
    return _files


def _json_response(payload, status=200):
    r = MagicMock()
    r.status_code = status
    r.json.return_value = payload
    r.raise_for_status.return_value = None
    return r


class TestUpload:
    def test_upload_hits_files_endpoint(self, files_mod):
        fake_file = MagicMock()
        fake_file.filename = "photo.jpg"
        fake_file.mimetype = "image/jpeg"
        fake_file.stream = BytesIO(b"\x89PNG")
        with patch("internal_api.requests.request") as fake_req, \
             patch("internal_api._get_headers", return_value={"Authorization": "Bearer t"}):
            fake_req.return_value = _json_response({"data": {"id": "f1", "name": "photo.jpg"}}, 201)
            out = files_mod.upload_file(fake_file, uploaded_by="me@x")
        assert out == {"id": "f1", "name": "photo.jpg"}
        args, kwargs = fake_req.call_args
        assert args[0] == "POST"
        assert "/api/files/my-tool" in args[1]
        assert kwargs["files"]["file"][0] == "photo.jpg"
        assert kwargs["data"] == {"uploaded_by": "me@x"}


class TestListAndDelete:
    def test_list_files(self, files_mod):
        with patch("internal_api.requests.request") as fake_req, \
             patch("internal_api._get_headers", return_value={}):
            fake_req.return_value = _json_response({"data": [{"id": "f1"}]})
            out = files_mod.list_files()
        assert out == [{"id": "f1"}]
        assert fake_req.call_args.args[0] == "GET"
        assert "/api/files/my-tool" in fake_req.call_args.args[1]

    def test_delete_file(self, files_mod):
        with patch("internal_api.requests.request") as fake_req, \
             patch("internal_api._get_headers", return_value={}):
            fake_req.return_value = _json_response({"data": {"deleted": True, "id": "f1"}})
            out = files_mod.delete_file("f1")
        assert out == {"deleted": True, "id": "f1"}
        assert fake_req.call_args.args[0] == "DELETE"
        assert "/api/files/my-tool/f1" in fake_req.call_args.args[1]


class TestDownload:
    def test_download_returns_bytes_and_content_type(self, files_mod):
        resp = MagicMock()
        resp.status_code = 200
        resp.content = b"hello-bytes"
        resp.headers = {"Content-Type": "image/png"}
        resp.raise_for_status.return_value = None
        with patch("internal_api.requests.request", return_value=resp), \
             patch("internal_api._get_headers", return_value={}):
            body, content_type = files_mod.download_file("f1")
        assert body == b"hello-bytes"
        assert content_type == "image/png"


class TestNoSlugGuard:
    def test_upload_without_tool_slug_raises(self, monkeypatch):
        monkeypatch.delenv("TOOL_SLUG", raising=False)
        import files as _files
        import internal_api as _api
        importlib.reload(_api)
        importlib.reload(_files)
        with pytest.raises(RuntimeError, match="TOOL_SLUG"):
            _files.upload_file(MagicMock(filename="x", mimetype="x", stream=BytesIO(b"")))
