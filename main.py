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
from internal_api import get

logging.basicConfig(level=logging.INFO)

app = Flask(__name__)
app.wsgi_app = ProxyFix(app.wsgi_app, x_proto=1, x_host=1)

JWT_SECRET = os.environ.get("JWT_SIGNING_SECRET", "")
AUTH_SERVICE_URL = "https://auth-service.storesight.org"
LOCAL_DEV = os.environ.get("LOCAL_DEV") == "1"
INTERNAL_API_BASE = os.environ.get("INTERNAL_API_BASE", "https://internal-tool-api.storesight.org")

# Cache for data (60s TTL)
_BLOOM_CACHE = {"jobs": None, "fetched_at": 0.0}
_PROJECTS_CACHE = {"data": {}, "fetched_at": 0.0}
_CACHE_TTL = 60


def _dev_token_path():
    """Return the path to the dev token file."""
    return pathlib.Path.home() / ".storesight" / "dev-token"


def _get_auth_header():
    """Return Authorization header for Internal API."""
    if LOCAL_DEV:
        token = _dev_token_path().read_text().strip()
    else:
        # In production, get OIDC token from Cloud Run metadata server
        try:
            import google.auth
            import google.auth.transport.requests
            credentials, _ = google.auth.default()
            request = google.auth.transport.requests.Request()
            credentials.refresh(request)
            token = credentials.token
        except Exception as e:
            logging.error(f"Failed to get OIDC token: {e}")
            token = ""
    return {"Authorization": f"Bearer {token}"}


def _fetch_response_groups():
    """Fetch response groups with submission timestamps from FieldAgent API."""
    now = time.time()
    if _BLOOM_CACHE["jobs"] and (now - _BLOOM_CACHE["fetched_at"]) < _CACHE_TTL:
        return _BLOOM_CACHE["jobs"]

    try:
        # Fetch response groups from last 10 days (covers all age buckets)
        from datetime import timedelta
        today = datetime.now().date()
        date_from = (today - timedelta(days=10)).isoformat()

        result = get(
            "/api/responsegroups",
            params={
                "submission_date_from": date_from,
                "per_page": 100,
                "sort": "-submission_date"
            }
        )
        groups = result.get("data", [])
        _BLOOM_CACHE["jobs"] = groups
        _BLOOM_CACHE["fetched_at"] = now
        logging.info(f"Fetched {len(groups)} response groups from FieldAgent")
        return groups
    except Exception as e:
        logging.error(f"Failed to fetch response groups: {e}")
        return _BLOOM_CACHE["jobs"] or []


def _parse_iso_datetime(dt_str):
    """Parse datetime string (RFC 2822 or ISO format), return seconds ago or None."""
    if not dt_str:
        return None
    try:
        # Try ISO format first
        try:
            dt = datetime.fromisoformat(dt_str.replace("Z", "+00:00"))
        except:
            # Try RFC 2822 format: "Fri, 07 Aug 2026 14:34:02 GMT"
            from email.utils import parsedate_to_datetime
            dt = parsedate_to_datetime(dt_str)

        # Make timezone-aware comparison
        now = datetime.now(dt.tzinfo) if dt.tzinfo else datetime.now()
        age_seconds = (now - dt).total_seconds()
        return max(0, age_seconds)
    except Exception as e:
        logging.warning(f"Failed to parse datetime '{dt_str}': {e}")
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


def _fetch_project_names():
    """Fetch project name mapping (project_id -> name)."""
    now = time.time()
    if _PROJECTS_CACHE["data"] and (now - _PROJECTS_CACHE["fetched_at"]) < _CACHE_TTL * 2:
        return _PROJECTS_CACHE["data"]

    try:
        result = get("/api/projects", params={"per_page": 500})
        projects = result.get("data", [])
        name_map = {str(p.get("id", "")): p.get("name", f"Project {p.get('id')}") for p in projects}
        _PROJECTS_CACHE["data"] = name_map
        _PROJECTS_CACHE["fetched_at"] = now
        logging.info(f"Fetched {len(name_map)} project names")
        return name_map
    except Exception as e:
        logging.warning(f"Failed to fetch project names: {e}")
        return _PROJECTS_CACHE["data"]


def _get_project_name(project_id):
    """Get project name by ID."""
    if not project_id:
        return "Unknown"
    projects = _fetch_project_names()
    return projects.get(str(project_id), f"Project {project_id}")


def _fetch_job_names():
    """Fetch job name mapping (job_id -> name)."""
    now = time.time()
    if _PROJECTS_CACHE.get("jobs") and (now - _PROJECTS_CACHE.get("jobs_fetched_at", 0)) < _CACHE_TTL * 2:
        return _PROJECTS_CACHE.get("jobs", {})

    try:
        result = get("/api/jobs", params={"per_page": 500})
        jobs = result.get("data", [])
        name_map = {str(j.get("id", "")): j.get("name", f"Job {j.get('id')}") for j in jobs}
        _PROJECTS_CACHE["jobs"] = name_map
        _PROJECTS_CACHE["jobs_fetched_at"] = now
        logging.info(f"Fetched {len(name_map)} job names")
        return name_map
    except Exception as e:
        logging.warning(f"Failed to fetch job names: {e}")
        return _PROJECTS_CACHE.get("jobs", {})


def _get_job_name(job_id):
    """Get job name by ID."""
    if not job_id:
        return "Unknown"
    jobs = _fetch_job_names()
    return jobs.get(str(job_id), f"Job {job_id}")


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
    """Return response groups with submission age, sorted by oldest first."""
    groups = _fetch_response_groups()

    # Group by job_id to get oldest submission per job
    jobs_map = {}
    for group in groups:
        job_id = group.get("job_id")
        if not job_id:
            continue

        # Skip reviewed groups
        first_review = group.get("first_review_ts")
        if first_review:
            continue

        # Skip test/screener/ticket jobs (status-based filtering)
        status = group.get("status", "")
        if status in ("D", "R"):  # Denied or rejected
            continue

        submission = group.get("submission_date")
        if not submission:
            continue

        if job_id not in jobs_map:
            jobs_map[job_id] = {
                "job_id": job_id,
                "project_id": group.get("project_id"),
                "submission_date": submission,
                "tp_review_company": group.get("tp_review_company"),
                "count": 0,
            }
        jobs_map[job_id]["count"] += 1

    result = []
    for job_id, job_data in jobs_map.items():
        age_seconds = _parse_iso_datetime(job_data["submission_date"])
        age_hours = _seconds_to_hours(age_seconds)

        result.append({
            "id": str(job_id),
            "jobName": _get_job_name(job_id),
            "projectId": job_data["project_id"],
            "projectName": _get_project_name(job_data["project_id"]),
            "vendor": job_data["tp_review_company"] or "Internal",
            "pendingCount": job_data["count"],
            "oldestSubmissionAge": age_hours,
            "oldestSubmissionStuck": age_hours >= 36 if age_hours else None,
        })

    # Sort by age (oldest first)
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
    groups = _fetch_response_groups()

    bottlenecks = defaultdict(lambda: {
        "pending": 0,
        "stuck": 0,
        "avgAge": 0,
        "jobCount": set(),
        "vendors": defaultdict(int),
        "project_id": None,
        "top_job_id": None,
        "top_job_pending": 0,
    })

    ages_by_project = defaultdict(list)

    for group in groups:
        # Skip reviewed groups
        if group.get("first_review_ts"):
            continue

        project_id = group.get("project_id", "unknown")
        project = _get_project_name(project_id)
        vendor = group.get("tp_review_company") or "Internal"
        submission = group.get("submission_date")
        job_id = group.get("job_id")

        age_seconds = _parse_iso_datetime(submission)
        age_hours = _seconds_to_hours(age_seconds)

        bottlenecks[project]["pending"] += 1
        bottlenecks[project]["project_id"] = project_id
        bottlenecks[project]["jobCount"].add(job_id)
        bottlenecks[project]["vendors"][vendor] += 1

        if age_hours and age_hours >= 36:
            bottlenecks[project]["stuck"] += 1

        if age_hours:
            ages_by_project[project].append(age_hours)

    # Find top job per project (most pending)
    for group in groups:
        if group.get("first_review_ts"):
            continue
        project = _get_project_name(group.get("project_id", "unknown"))
        job_id = group.get("job_id")
        if project in bottlenecks:
            # Count pending per job for this project
            job_key = f"{project}_{job_id}"
            if not hasattr(api_u36_bottlenecks, "_job_counts"):
                api_u36_bottlenecks._job_counts = defaultdict(int)
            api_u36_bottlenecks._job_counts[job_key] += 1

            if api_u36_bottlenecks._job_counts[job_key] > bottlenecks[project]["top_job_pending"]:
                bottlenecks[project]["top_job_pending"] = api_u36_bottlenecks._job_counts[job_key]
                bottlenecks[project]["top_job_id"] = job_id

    # Calculate average age per project
    for project, ages in ages_by_project.items():
        if ages:
            bottlenecks[project]["avgAge"] = round(sum(ages) / len(ages), 1)

    result = [
        {
            "project": project,
            "projectId": data["project_id"],
            "topJobId": data["top_job_id"],
            "pendingSubmissions": data["pending"],
            "jobsStuck": data["stuck"],
            "jobCount": len(data["jobCount"]),
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
    """Return response groups stuck >36 hours."""
    groups = _fetch_response_groups()

    alerts_map = {}
    for group in groups:
        # Skip reviewed groups
        if group.get("first_review_ts"):
            continue

        submission = group.get("submission_date")
        job_id = group.get("job_id")
        group_id = group.get("id")

        age_seconds = _parse_iso_datetime(submission)
        age_hours = _seconds_to_hours(age_seconds)

        if not (age_hours and age_hours >= 36):
            continue

        # Group by job to deduplicate, track oldest group_id
        if job_id not in alerts_map:
            alerts_map[job_id] = {
                "job_id": job_id,
                "project_id": group.get("project_id"),
                "vendor": group.get("tp_review_company") or "Internal",
                "age_hours": age_hours,
                "group_id": group_id,
                "count": 0,
            }
        else:
            # Keep the oldest (highest age)
            if age_hours > alerts_map[job_id]["age_hours"]:
                alerts_map[job_id]["age_hours"] = age_hours
                alerts_map[job_id]["group_id"] = group_id
        alerts_map[job_id]["count"] += 1

    alerts = [
        {
            "id": str(alert["job_id"]),
            "projectName": _get_project_name(alert["project_id"]),
            "vendor": alert["vendor"],
            "pendingCount": alert["count"],
            "stuckHours": alert["age_hours"],
            "groupId": alert["group_id"],
            "severity": "critical" if alert["age_hours"] >= 72 else "warning",
        }
        for alert in alerts_map.values()
    ]

    # Sort by hours stuck (most critical first)
    alerts.sort(key=lambda x: -x["stuckHours"])

    logging.info(f"GET /api/u36/alerts by={g.user.get('email')} count={len(alerts)}")
    return jsonify({"data": alerts})


@app.route("/api/u36/late-reviews")
def api_u36_late_reviews():
    """Return jobs reviewed after 36 hours (TAT violations)."""
    groups = _fetch_response_groups()

    logging.info(f"Late reviews: checking {len(groups)} groups")

    violations_map = {}
    reviewed_count = 0
    for group in groups:
        submission = group.get("submission_date")
        review_time = group.get("first_review_ts")
        job_id = group.get("job_id")
        group_id = group.get("id")

        # Only include reviewed groups
        if not review_time:
            continue

        reviewed_count += 1

        # Calculate time to review
        try:
            from email.utils import parsedate_to_datetime

            # Parse submission time
            try:
                sub_dt = datetime.fromisoformat(submission.replace("Z", "+00:00"))
            except:
                sub_dt = parsedate_to_datetime(submission) if submission else None

            # Parse review time
            try:
                rev_dt = datetime.fromisoformat(review_time.replace("Z", "+00:00"))
            except:
                rev_dt = parsedate_to_datetime(review_time) if review_time else None

            if not sub_dt or not rev_dt:
                continue

            tat_seconds = (rev_dt - sub_dt).total_seconds()
            tat_hours = _seconds_to_hours(tat_seconds)
        except Exception as e:
            logging.warning(f"Failed to calc TAT for group {group_id}: {e}")
            continue

        if not (tat_hours and tat_hours >= 36):
            continue

        # Group by job, track worst (longest TAT) group_id
        if job_id not in violations_map:
            violations_map[job_id] = {
                "job_id": job_id,
                "project_id": group.get("project_id"),
                "vendor": group.get("tp_review_company") or "Internal",
                "tat_hours": tat_hours,
                "group_id": group_id,
                "count": 0,
            }
        else:
            if tat_hours > violations_map[job_id]["tat_hours"]:
                violations_map[job_id]["tat_hours"] = tat_hours
                violations_map[job_id]["group_id"] = group_id
        violations_map[job_id]["count"] += 1

    violations = [
        {
            "id": str(v["job_id"]),
            "projectName": _get_project_name(v["project_id"]),
            "vendor": v["vendor"],
            "responseCount": v["count"],
            "tatHours": v["tat_hours"],
            "groupId": v["group_id"],
            "severity": "critical" if v["tat_hours"] >= 72 else "warning",
        }
        for v in violations_map.values()
    ]

    # Sort by TAT hours (worst first)
    violations.sort(key=lambda x: -x["tatHours"])

    logging.info(f"GET /api/u36/late-reviews by={g.user.get('email')} reviewed={reviewed_count} violations={len(violations)}")
    return jsonify({"data": violations})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
