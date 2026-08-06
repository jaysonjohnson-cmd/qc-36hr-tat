"""Structural invariants for the platform template.

Bloom (the developer portal) copies this repo to create every new tool, then
depends on certain files existing and carrying certain load-bearing content —
`main.py`'s `/health` route and auth guard, the packages in `requirements.txt`,
and the substitution placeholders in `AGENTS.md`. If any of those silently
drift, new tools break (or, for `AGENTS.md`, every tool's guidance does). These
tests guard that contract so a bad edit fails CI *here*, before it ships to the
fleet. Companion to Bloom's own contract test (`tests/test_files.py`).
"""

import pathlib
import re

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]


def _read(name):
    return (ROOT / name).read_text(encoding="utf-8")


# Files Bloom's provisioning / upload code assumes exist at the repo root.
# (Bloom reads/patches cloudbuild.yaml, README.md and AGENTS.md, and BLOOM-45
# protects main.py/internal_api.py/Dockerfile/cloudbuild.yaml/requirements.txt/
# get-dev-token.py from being clobbered by an upload.)
REQUIRED_FILES = [
    "main.py",
    "internal_api.py",
    "Dockerfile",
    "cloudbuild.yaml",
    "requirements.txt",
    "get-dev-token.py",
    "AGENTS.md",
    "README.md",
]

# Packages the platform scaffolding needs to boot; a tool that drops these will
# not start. Keep in sync with requirements.txt.
REQUIRED_PACKAGES = ["flask", "gunicorn", "google-auth", "PyJWT", "requests"]

# Placeholders that Bloom's `_generate_agents_md()` substitutes per tool. If any
# go missing from AGENTS.md, the substitution silently no-ops and every new tool
# ships with a broken guide (e.g. the agent can't discover its Drive folder).
AGENTS_PLACEHOLDERS = ["[TOOL NAME]", "[your-slug]", "[drive-folder-id]"]


@pytest.mark.parametrize("name", REQUIRED_FILES)
def test_required_file_exists(name):
    assert (ROOT / name).is_file(), f"Template is missing a required file: {name}"


def test_main_has_health_route():
    src = _read("main.py")
    assert '@app.route("/health")' in src, (
        "main.py must expose a /health route — Cloud Run's health check hits it "
        "and provisioning relies on the service coming up healthy."
    )


def test_main_has_auth_guard():
    src = _read("main.py")
    assert "@app.before_request" in src and "require_auth" in src, (
        "main.py must keep its before_request auth guard — dropping it would "
        "deploy tools with no authentication."
    )


@pytest.mark.parametrize("pkg", REQUIRED_PACKAGES)
def test_requirements_pins_platform_package(pkg):
    reqs = _read("requirements.txt")
    assert re.search(rf"(?im)^{re.escape(pkg)}==", reqs), (
        f"requirements.txt must pin the platform package '{pkg}==' — the "
        "scaffolding will not boot without it."
    )


@pytest.mark.parametrize("token", AGENTS_PLACEHOLDERS)
def test_agents_md_has_substitution_placeholder(token):
    assert token in _read("AGENTS.md"), (
        f"AGENTS.md must contain the {token} placeholder that Bloom substitutes "
        "per tool; without it the substitution silently no-ops."
    )


def test_agents_md_has_pending_migrations_section():
    assert "### Pending Migrations" in _read("AGENTS.md"), (
        "AGENTS.md must keep the 'Pending Migrations' section — it is the only "
        "channel that reaches already-provisioned tools."
    )
