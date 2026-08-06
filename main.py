import logging
import os
import pathlib
import time
from datetime import datetime
from collections import defaultdict

import jwt
import requests
from flask import Flask, jsonify, redirect, request, g, send_file
from werkzeug.middleware.proxy_fix import ProxyFix

logging.basicConfig(level=logging.INFO)

app = Flask(__name__)
app.wsgi_app = ProxyFix(app.wsgi_app, x_proto=1, x_host=1)

JWT_SECRET = os.environ.get("JWT_SIGNING_SECRET", "")
AUTH_SERVICE_URL = "https://auth-service.storesight.org"
LOCAL_DEV = os.environ.get("LOCAL_DEV") == "1"
INTERNAL_API_BASE = os.environ.get("INTERNAL_API_BASE", "https://internal-tool-api.storesight.org")

# Cache for Bloom data (60s TTL)
_BLOOM_CACHE = {"jobs": None, "fetched_at": 0.0}
_CACHE_TTL = 60


def _dev_token_path():
    """Return the path to the dev token file."""
    return pathlib.Path.home() / ".storesight" / "dev-token"


def _get_auth_header():
    """Return Authorization header for Internal API."""
    if LOCAL_DEV:
        token = _dev_token_path().read_text().strip()
    else:
        token = os.environ.get("OIDC_TOKEN", "")
    return {"Authorization": f"Bearer {token}"}


def _fetch_bloom_jobs():
    """Fetch prioritized jobs from Bloom API via Internal API."""
    now = time.time()
    if _BLOOM_CACHE["jobs"] and (now - _BLOOM_CACHE["fetched_at"]) < _CACHE_TTL:
        return _BLOOM_CACHE["jobs"]

    try:
        resp = requests.get(
            f"{INTERNAL_API_BASE}/api/prioritized-jobs",
            headers=_get_auth_header(),
            timeout=30
        )
        resp.raise_for_status()
        jobs = resp.json().get("data", [])
        _BLOOM_CACHE["jobs"] = jobs
        _BLOOM_CACHE["fetched_at"] = now
        logging.info(f"Fetched {len(jobs)} jobs from Bloom")
        return jobs
    except Exception as e:
        logging.error(f"Failed to fetch Bloom jobs: {e}")
        return _BLOOM_CACHE["jobs"] or []


def _parse_iso_datetime(dt_str):
    """Parse ISO datetime string, return seconds ago or None."""
    if not dt_str:
        return None
    try:
        dt = datetime.fromisoformat(dt_str.replace("Z", "+00:00"))
        age_seconds = (datetime.now(dt.tzinfo) - dt).total_seconds()
        return max(0, age_seconds)
    except:
        return None


def _seconds_to_hours(seconds):
    """Convert seconds to hours, rounded to 1 decimal."""
    if seconds is None:
        return None
    return round(seconds / 3600, 1)


def _get_project_name(job):
    """Extract project name from job."""
    return job.get("project_name") or f"Project {job.get('project_id', 'unknown')}"


def _get_vendor(job):
    """Extract review vendor (internal or third-party)."""
    vendor = job.get("tp_review_company") or ""
    return vendor if vendor else "Internal"


@app.before_request
def require_auth():
    if request.path == "/health":
        return

    if LOCAL_DEV:
        # Local development: read identity from dev token file
        token_file = _dev_token_path()
        try:
            token_str = token_file.read_text().strip()
        except FileNotFoundError:
            return (
                "<h1>Dev token not found</h1>"
                "<p>No dev token at ~/.storesight/dev-token. "
                "Run the dev token setup flow to authenticate.</p>"
            ), 401

        if not token_str:
            return (
                "<h1>Dev token not found</h1>"
                "<p>Dev token file is empty. Re-run the setup flow.</p>"
            ), 401

        # Decode without verifying signature (no JWT_SIGNING_SECRET locally).
        # Check expiry manually.
        try:
            payload = jwt.decode(
                token_str, options={"verify_signature": False, "verify_aud": False, "verify_exp": False}
            )
        except jwt.InvalidTokenError:
            return "<h1>Invalid dev token</h1><p>Re-run the setup flow.</p>", 401

        if payload.get("exp", 0) < time.time():
            return (
                "<h1>Dev token expired</h1>"
                "<p>Your dev token has expired. Re-authenticate by running the setup flow.</p>"
            ), 401

        g.user = {"email": payload.get("email", ""), "name": payload.get("name", "")}
        return

    # Production: validate storesight_session cookie
    token = request.cookies.get("storesight_session")
    if not token:
        return redirect(f"{AUTH_SERVICE_URL}/login?return_url={request.url}")
    try:
        payload = jwt.decode(token, JWT_SECRET, algorithms=["HS256"])
        g.user = {"email": payload["email"], "name": payload.get("name", "")}
    except (jwt.ExpiredSignatureError, jwt.InvalidTokenError):
        return redirect(f"{AUTH_SERVICE_URL}/login?return_url={request.url}")


@app.route("/health")
def health():
    return jsonify({"status": "ok"})


@app.route("/logout")
def logout():
    return redirect(f"{AUTH_SERVICE_URL}/logout?return_url={request.url_root}")


@app.route("/")
def index():
    return send_file("templates/index.html", mimetype="text/html")


@app.route("/api/u36/jobs")
def api_u36_jobs():
    """Return jobs with submission age, sorted by oldest first."""
    jobs = _fetch_bloom_jobs()

    result = []
    for job in jobs:
        new_count = int(job.get("new") or 0)
        if new_count == 0:
            continue

        oldest_sub = job.get("oldestSubmission")
        age_seconds = _parse_iso_datetime(oldest_sub)
        age_hours = _seconds_to_hours(age_seconds)

        result.append({
            "id": str(job.get("id", "")),
            "name": job.get("name", ""),
            "projectId": job.get("project_id", ""),
            "projectName": _get_project_name(job),
            "vendor": _get_vendor(job),
            "pendingCount": new_count,
            "oldestSubmissionAge": age_hours,
            "oldestSubmissionStuck": age_hours >= 36 if age_hours else None,
            "activeReviewers": job.get("activeReviewers", 0),
            "priority": job.get("priority", 0),
        })

    # Sort by age (oldest first), then by pending count
    result.sort(key=lambda x: (
        x["oldestSubmissionAge"] is None,
        -(x["oldestSubmissionAge"] or 0),
        -x["pendingCount"]
    ))

    logging.info(f"GET /api/u36/jobs by={g.user.get('email')} count={len(result)}")
    return jsonify({"data": result})


@app.route("/api/u36/bottlenecks")
def api_u36_bottlenecks():
    """Return bottleneck analysis by project and vendor."""
    jobs = _fetch_bloom_jobs()

    bottlenecks = defaultdict(lambda: {
        "pending": 0,
        "stuck": 0,
        "avgAge": 0,
        "jobCount": 0,
        "vendors": defaultdict(int),
    })

    ages_by_project = defaultdict(list)

    for job in jobs:
        new_count = int(job.get("new") or 0)
        if new_count == 0:
            continue

        project = _get_project_name(job)
        vendor = _get_vendor(job)
        oldest_sub = job.get("oldestSubmission")
        age_seconds = _parse_iso_datetime(oldest_sub)
        age_hours = _seconds_to_hours(age_seconds)

        bottlenecks[project]["pending"] += new_count
        bottlenecks[project]["jobCount"] += 1
        bottlenecks[project]["vendors"][vendor] += 1

        if age_hours and age_hours >= 36:
            bottlenecks[project]["stuck"] += 1

        if age_hours:
            ages_by_project[project].append(age_hours)

    # Calculate average age per project
    for project, ages in ages_by_project.items():
        if ages:
            bottlenecks[project]["avgAge"] = round(sum(ages) / len(ages), 1)

    result = [
        {
            "project": project,
            "pendingSubmissions": data["pending"],
            "jobsStuck": data["stuck"],
            "jobCount": data["jobCount"],
            "avgAge": data["avgAge"],
            "vendors": dict(data["vendors"]),
        }
        for project, data in bottlenecks.items()
    ]

    # Sort by pending count (most problematic first)
    result.sort(key=lambda x: -x["pendingSubmissions"])

    logging.info(f"GET /api/u36/bottlenecks by={g.user.get('email')} projects={len(result)}")
    return jsonify({"data": result})


@app.route("/api/u36/alerts")
def api_u36_alerts():
    """Return jobs stuck >36 hours with pending submissions."""
    jobs = _fetch_bloom_jobs()

    alerts = []
    for job in jobs:
        new_count = int(job.get("new") or 0)
        if new_count == 0:
            continue

        oldest_sub = job.get("oldestSubmission")
        age_seconds = _parse_iso_datetime(oldest_sub)
        age_hours = _seconds_to_hours(age_seconds)

        if age_hours and age_hours >= 36:
            alerts.append({
                "id": str(job.get("id", "")),
                "name": job.get("name", ""),
                "projectName": _get_project_name(job),
                "vendor": _get_vendor(job),
                "pendingCount": new_count,
                "stuckHours": age_hours,
                "activeReviewers": job.get("activeReviewers", 0),
                "severity": "critical" if age_hours >= 72 else "warning",
            })

    # Sort by hours stuck (most critical first)
    alerts.sort(key=lambda x: -x["stuckHours"])

    logging.info(f"GET /api/u36/alerts by={g.user.get('email')} count={len(alerts)}")
    return jsonify({"data": alerts})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
