"""Thin wrapper around the Internal API's per-tool file storage.

Every Bloom-provisioned tool gets a private Google Drive folder created at
provisioning time. This module is the only path your tool should use to
read or write files in that folder — the Drive credentials live inside the
Internal API and never leave it.

Usage:
    from files import upload_file, list_files, download_file, delete_file

    # Upload an incoming Flask FileStorage
    metadata = upload_file(request.files["file"], uploaded_by=g.user["email"])

    # List everything the tool has stored
    for f in list_files():
        print(f["name"], f["size"])

    # Stream a file back to the user
    body, content_type = download_file(metadata["id"])

    # Remove a file
    delete_file(metadata["id"])
"""
import os

import internal_api

TOOL_SLUG = os.environ.get("TOOL_SLUG", "")


def _base():
    if not TOOL_SLUG:
        raise RuntimeError("TOOL_SLUG is not set — set it before calling the Files API")
    return f"/api/files/{TOOL_SLUG}"


def upload_file(file_storage, *, uploaded_by=""):
    """Upload a Werkzeug FileStorage to this tool's Drive folder.

    `file_storage` is what you get from `request.files["file"]`. Returns the
    file's metadata dict — store the `id` if you need to fetch the file
    again later.
    """
    filename = (file_storage.filename or "untitled").strip() or "untitled"
    mimetype = file_storage.mimetype or "application/octet-stream"
    resp = internal_api.post_multipart(
        _base(),
        files={"file": (filename, file_storage.stream, mimetype)},
        data={"uploaded_by": uploaded_by} if uploaded_by else None,
    )
    return resp["data"]


def list_files():
    """List every file in this tool's folder, newest first."""
    return internal_api.get(_base())["data"]


def file_meta(file_id):
    """Return a single file's metadata (no body)."""
    return internal_api.get(f"{_base()}/{file_id}/meta")["data"]


def download_file(file_id):
    """Return (bytes, content_type) for a file in this tool's folder."""
    return internal_api.get_bytes(f"{_base()}/{file_id}")


def delete_file(file_id):
    """Delete a file from this tool's folder."""
    return internal_api.delete(f"{_base()}/{file_id}")["data"]
