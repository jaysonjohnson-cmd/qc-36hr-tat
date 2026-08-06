"""Thin wrapper for calling the Internal Tool API with automatic Cloud Run auth."""

import os
import logging
import pathlib
import threading
import time

import requests
import google.auth.transport.requests
import google.oauth2.id_token

INTERNAL_API_BASE = os.environ.get("INTERNAL_API_BASE", "https://internal-tool-api.storesight.org")
TOOL_SLUG = os.environ.get("TOOL_SLUG", "")
LOCAL_DEV = os.environ.get("LOCAL_DEV") == "1"

_auth_request = google.auth.transport.requests.Request()


def _dev_token_path():
    """Return the path to the dev token file."""
    return pathlib.Path.home() / ".storesight" / "dev-token"


# OIDC identity tokens are valid ~1h. Caching turns a per-request call to the
# metadata server (the source of intermittent, silently-swallowed 401s) into
# roughly one call per hour, and removes a network round-trip from the hot path.
_token_lock = threading.Lock()
_token_cache = {"value": None, "exp": 0.0}
_TOKEN_TTL = 3000  # seconds (~50 min); comfortably under the ~1h token life
_FETCH_RETRY_DELAYS = (0, 0.25, 0.75)  # brief backoff so a transient hiccup retries


def _identity_token():
    """Return a cached OIDC identity token for the Internal API, refreshing only
    near expiry.

    Retries briefly so a transient metadata-server hiccup doesn't slip through
    as an unauthenticated request, and raises if a token genuinely can't be
    obtained — failing loud rather than silently sending no auth (which
    surfaces downstream as a confusing 401).
    """
    if _token_cache["value"] and time.time() < _token_cache["exp"]:
        return _token_cache["value"]

    with _token_lock:
        # Re-check under the lock so concurrent threads mint at most once.
        if _token_cache["value"] and time.time() < _token_cache["exp"]:
            return _token_cache["value"]

        last_err = None
        for delay in _FETCH_RETRY_DELAYS:
            if delay:
                time.sleep(delay)
            try:
                token = google.oauth2.id_token.fetch_id_token(
                    _auth_request, INTERNAL_API_BASE
                )
                _token_cache["value"] = token
                _token_cache["exp"] = time.time() + _TOKEN_TTL
                return token
            except Exception as e:  # transient metadata-server failure
                last_err = e

        raise RuntimeError(
            "Could not mint an OIDC identity token for the Internal API"
        ) from last_err


def _get_headers():
    """Return auth headers for an Internal API call.

    Production (Cloud Run): a cached OIDC identity token from the metadata
    server. Local dev (LOCAL_DEV=1): the dev token at ~/.storesight/dev-token.

    Raises if no token can be obtained rather than silently sending an
    unauthenticated request.
    """
    headers = {}
    if TOOL_SLUG:
        headers["X-Tool-Slug"] = TOOL_SLUG

    if LOCAL_DEV:
        try:
            token = _dev_token_path().read_text().strip()
        except FileNotFoundError:
            token = ""
        if not token:
            raise RuntimeError(
                "No dev token at ~/.storesight/dev-token. Run the dev token "
                "setup flow to authenticate."
            )
    else:
        token = _identity_token()

    headers["Authorization"] = f"Bearer {token}"
    return headers


def _ensure_trailing_slash(path):
    """Ensure path ends with / to avoid Flask 308 redirects that strip auth headers."""
    if not path.endswith("/"):
        path += "/"
    return path


_MAX_RETRIES = 3
_RETRY_BACKOFF = [1, 2, 4]  # seconds to wait between retries on 429


def _request(method, path, **kwargs):
    """Send a request with automatic retry on 429 (rate limit).

    The API enforces per-tool rate limits (60 requests/minute by default,
    keyed on the X-Tool-Slug header). If you hit the limit, this helper
    waits and retries up to 3 times with exponential backoff.
    """
    url = f"{INTERNAL_API_BASE}{_ensure_trailing_slash(path)}"
    kwargs.setdefault("headers", _get_headers())
    kwargs.setdefault("timeout", 30)

    for attempt in range(_MAX_RETRIES + 1):
        resp = requests.request(method, url, **kwargs)
        if resp.status_code != 429 or attempt == _MAX_RETRIES:
            resp.raise_for_status()
            return resp.json()
        wait = _RETRY_BACKOFF[attempt]
        logging.warning(
            "Rate limited (429) on %s %s — retrying in %ds (attempt %d/%d)",
            method.upper(), path, wait, attempt + 1, _MAX_RETRIES,
        )
        time.sleep(wait)


def get(path, params=None):
    """GET a path on the Internal API. Returns the parsed JSON response.

    Usage:
        from internal_api import get
        data = get("/api/agents", params={"page": 1})
    """
    return _request("GET", path, params=params)


def post(path, json=None):
    """POST to the Internal API. Returns the parsed JSON response.

    Usage:
        from internal_api import post
        result = post("/api/slack/post", json={"channel": "C01ABCDEF", "text": "Hello!"})
    """
    return _request("POST", path, json=json)


def put(path, json=None):
    """PUT to the Internal API. Returns the parsed JSON response.

    Usage:
        from internal_api import put
        result = put("/api/storage/my-tool/abc123", json={"data": {"name": "updated"}})
    """
    return _request("PUT", path, json=json)


def delete(path):
    """DELETE on the Internal API. Returns the parsed JSON response.

    Usage:
        from internal_api import delete
        result = delete("/api/storage/my-tool/abc123")
    """
    return _request("DELETE", path)


def post_multipart(path, files=None, data=None):
    """POST a multipart/form-data body. Returns the parsed JSON response.

    `files` should be a dict mapping field names to `(filename, fileobj, mimetype)`
    tuples, matching the `requests` library convention.

    Usage:
        from internal_api import post_multipart
        with open("photo.jpg", "rb") as f:
            result = post_multipart(
                "/api/files/my-tool",
                files={"file": ("photo.jpg", f, "image/jpeg")},
                data={"uploaded_by": g.user["email"]},
            )
    """
    return _request("POST", path, files=files, data=data)


def get_bytes(path, params=None):
    """GET a path and return raw response bytes plus content-type.

    Use for binary downloads (e.g. /api/files/<slug>/<id>) where the body
    is not JSON. Honors the same auth + retry behavior as `get`.
    """
    url = f"{INTERNAL_API_BASE}{_ensure_trailing_slash(path)}"
    headers = _get_headers()
    for attempt in range(_MAX_RETRIES + 1):
        resp = requests.request("GET", url, params=params, headers=headers, timeout=30)
        if resp.status_code != 429 or attempt == _MAX_RETRIES:
            resp.raise_for_status()
            return resp.content, resp.headers.get("Content-Type", "application/octet-stream")
        wait = _RETRY_BACKOFF[attempt]
        logging.warning(
            "Rate limited (429) on GET %s — retrying in %ds (attempt %d/%d)",
            path, wait, attempt + 1, _MAX_RETRIES,
        )
        time.sleep(wait)
