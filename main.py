import logging
import os
import pathlib
import threading
import time
from datetime import datetime, timezone
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
_NAMES_CACHE = {}  # "projects" / "jobs" -> {id: name}; names never change, so no TTL
_CACHE_TTL = 60


def _dev_token_path():
    """Return the path to the dev token file."""
    return pathlib.Path.home() / ".storesight" / "dev-token"


_MAX_RESPONSE_GROUP_PAGES = 100  # ~10000 records at 100/page (API caps at 100); ensure we reach older jobs
_response_groups_lock = threading.Lock()


def _fetch_response_groups():
    """Fetch response groups with submission timestamps from FieldAgent API.

    The API caps per_page at 100 regardless of what we ask for, and returns
    newest-first, so a single request only ever gets today's submissions.
    Page through until we either run out of results or hit the page cap.

    The 4 dashboard tabs all call this on every refresh. Without
    coordination, each would kick off its own multi-page sweep at the same
    time and blow through the API's 60 req/min per-tool quota. A lock makes
    concurrent callers share one sweep instead of racing to start their own.
    """
    now = time.time()
    if _BLOOM_CACHE["jobs"] and (now - _BLOOM_CACHE["fetched_at"]) < _CACHE_TTL:
        return _BLOOM_CACHE["jobs"]

    with _response_groups_lock:
        # Re-check now that we hold the lock — another thread may have just
        # finished the sweep while we were waiting.
        now = time.time()
        if _BLOOM_CACHE["jobs"] and (now - _BLOOM_CACHE["fetched_at"]) < _CACHE_TTL:
            return _BLOOM_CACHE["jobs"]

        try:
            all_groups = []
            for page in range(1, _MAX_RESPONSE_GROUP_PAGES + 1):
                result = get(
                    "/api/responsegroups",
                    params={
                        "per_page": 100,  # API max
                        "page": page,
                        "sort": "-submission_date",
                        "status": "N"
                    }
                )
                page_groups = result.get("data", [])
                if not page_groups:
                    break
                all_groups.extend(page_groups)

                if len(page_groups) < 100:
                    break
                # Small pacing gap between our own page requests so a single
                # sweep doesn't itself look like a burst to the rate limiter.
                time.sleep(0.2)

            _BLOOM_CACHE["jobs"] = all_groups
            _BLOOM_CACHE["fetched_at"] = time.time()
            if all_groups:
                oldest = all_groups[-1].get("submission_date")
                oldest_age_hours = _seconds_to_hours(_parse_iso_datetime(oldest))
                logging.info(f"Successfully fetched {len(all_groups)} response groups. Oldest: {oldest_age_hours}h old")
            else:
                logging.info(f"Successfully fetched {len(all_groups)} response groups across pages")
            return all_groups
        except RuntimeError as e:
            logging.error(f"AUTHENTICATION FAILED: {e}")
            logging.error(f"This usually means: no dev token, token expired, or OIDC token unavailable")
            return []
        except Exception as e:
            logging.error(f"Failed to fetch response groups: {type(e).__name__}: {e}")
            import traceback
            logging.error(traceback.format_exc())
            return []


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

        # Timestamps without a timezone are UTC — compare against UTC, not the
        # server's local clock (which would skew ages by hours when run locally).
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        age_seconds = (datetime.now(timezone.utc) - dt).total_seconds()
        return max(0, age_seconds)
    except Exception as e:
        logging.warning(f"Failed to parse datetime '{dt_str}': {e}")
        return None


def _seconds_to_hours(seconds):
    """Convert seconds to hours, rounded to 1 decimal."""
    if seconds is None:
        return None
    return round(seconds / 3600, 1)


_names_lock = threading.Lock()
_NAME_LOOKUPS_PER_MINUTE = 30  # leaves headroom under the 60 req/min tool quota
_name_lookup_window = {"start": 0.0, "used": 0}


def _lookup_name(kind, path, id_param, name_field, item_id):
    """Look up one job or project name by ID, caching it for the life of the process.

    There are millions of jobs/projects, so we can't download a full id -> name
    list, and the API doesn't accept multiple IDs per request. Instead we look
    up only the IDs the dashboard actually shows. Names don't change, so each
    ID costs one API call ever. Lookups are capped per minute so a cold start
    can't eat the rate limit; anything over the cap returns None (the caller
    shows a placeholder) and is filled in on a later refresh.
    """
    key = str(item_id)
    cache = _NAMES_CACHE.setdefault(kind, {})
    if key in cache:
        return cache[key]

    with _names_lock:
        if key in cache:
            return cache[key]

        now = time.time()
        if now - _name_lookup_window["start"] >= 60:
            _name_lookup_window["start"] = now
            _name_lookup_window["used"] = 0
        if _name_lookup_window["used"] >= _NAME_LOOKUPS_PER_MINUTE:
            return None
        _name_lookup_window["used"] += 1

        try:
            result = get(path, params={id_param: item_id, "per_page": 1})
            items = result.get("data", [])
            cache[key] = items[0].get(name_field) if items else None
        except Exception as e:
            # Not cached, so it's retried on a later refresh (still within the cap).
            logging.warning(f"Failed to look up {kind} name for {item_id}: {e}")
            return None
        return cache[key]


def _get_project_name(project_id):
    """Get project name by ID."""
    if not project_id:
        return "Unknown"
    name = _lookup_name("projects", "/api/projects", "project_id", "title", project_id)
    return name or f"Project {project_id}"


def _get_job_name(job_id):
    """Get job name by ID."""
    if not job_id:
        return "Unknown"
    name = _lookup_name("jobs", "/api/jobs", "job_id", "name", job_id)
    return name or f"Job {job_id}"


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

        age_seconds = _parse_iso_datetime(submission)

        if job_id not in jobs_map:
            jobs_map[job_id] = {
                "job_id": job_id,
                "project_id": group.get("project_id"),
                "age_seconds": age_seconds,
                "tp_review_company": group.get("tp_review_company"),
                "count": 0,
            }
        elif age_seconds is not None and (
            jobs_map[job_id]["age_seconds"] is None or age_seconds > jobs_map[job_id]["age_seconds"]
        ):
            # Keep the oldest submission — the API returns newest first, so
            # the first one we see for a job is usually its newest.
            jobs_map[job_id]["age_seconds"] = age_seconds
        jobs_map[job_id]["count"] += 1

    result = []
    for job_id, job_data in jobs_map.items():
        age_hours = _seconds_to_hours(job_data["age_seconds"])

        result.append({
            "id": str(job_id),
            "jobName": _get_job_name(job_id),
            "projectId": job_data["project_id"],
            "projectName": _get_project_name(job_data["project_id"]),
            "vendor": job_data["tp_review_company"] or "Internal",
            "pendingCount": job_data["count"],
            "oldestSubmissionAge": age_hours,
            "oldestSubmissionStuck": age_hours >= 36 if age_hours is not None else None,
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

        if age_hours is not None:
            ages_by_project[project].append(age_hours)

    # Find top job per project (most pending). Counts are local to this
    # request so they start fresh on every refresh.
    job_counts = defaultdict(int)
    for group in groups:
        if group.get("first_review_ts"):
            continue
        project = _get_project_name(group.get("project_id", "unknown"))
        job_id = group.get("job_id")
        if project in bottlenecks:
            job_key = (project, job_id)
            job_counts[job_key] += 1

            if job_counts[job_key] > bottlenecks[project]["top_job_pending"]:
                bottlenecks[project]["top_job_pending"] = job_counts[job_key]
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
    """Return response groups at-risk (28h+) and stuck (36h+)."""
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

        if not (age_hours and age_hours >= 28):
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
            "name": _get_job_name(alert["job_id"]),
            "projectName": _get_project_name(alert["project_id"]),
            "vendor": alert["vendor"],
            "pendingCount": alert["count"],
            "stuckHours": alert["age_hours"],
            "groupId": alert["group_id"],
            "severity": "critical" if alert["age_hours"] >= 36 else "at-risk",
        }
        for alert in alerts_map.values()
    ]

    # Sort by hours stuck (most critical first)
    alerts.sort(key=lambda x: -x["stuckHours"])

    logging.info(f"GET /api/u36/alerts by={g.user.get('email')} count={len(alerts)}")
    return jsonify({"data": alerts})


@app.route("/api/u36/late-reviews")
def api_u36_late_reviews():
    """Return jobs that are TAT violations against the 36hr KPI.

    A violation is either:
      - reviewed-late: first_review_ts is set, but occurred >36h after submission
      - still-pending: first_review_ts is NULL, and it's been >36h since submission
    (Per the U36 KPI definition: on-time = first_review_ts within 36h of submission.)
    """
    groups = _fetch_response_groups()

    violations_map = {}
    reviewed_late_count = 0
    still_pending_count = 0

    for group in groups:
        submission = group.get("submission_date")
        review_time = group.get("first_review_ts")
        job_id = group.get("job_id")
        group_id = group.get("id")

        if not submission or not job_id:
            continue

        sub_age_seconds = _parse_iso_datetime(submission)
        sub_age_hours = _seconds_to_hours(sub_age_seconds)

        if review_time:
            # Reviewed — check if it happened more than 36h after submission
            review_age_seconds = _parse_iso_datetime(review_time)
            if review_age_seconds is None or sub_age_seconds is None:
                continue
            tat_hours = _seconds_to_hours(sub_age_seconds - review_age_seconds)
            if tat_hours is None or tat_hours < 36:
                continue
            status = "reviewed-late"
            reviewed_late_count += 1
        else:
            # Still pending — check if it's been more than 36h since submission
            if sub_age_hours is None or sub_age_hours < 36:
                continue
            tat_hours = sub_age_hours
            status = "still-pending"
            still_pending_count += 1

        # Group by job, track worst (longest TAT) group_id
        if job_id not in violations_map:
            violations_map[job_id] = {
                "job_id": job_id,
                "project_id": group.get("project_id"),
                "vendor": group.get("tp_review_company") or "Internal",
                "tat_hours": tat_hours,
                "status": status,
                "group_id": group_id,
                "count": 0,
            }
        else:
            if tat_hours > violations_map[job_id]["tat_hours"]:
                violations_map[job_id]["tat_hours"] = tat_hours
                violations_map[job_id]["status"] = status
                violations_map[job_id]["group_id"] = group_id
        violations_map[job_id]["count"] += 1

    violations = [
        {
            "id": str(v["job_id"]),
            "name": _get_job_name(v["job_id"]),
            "projectName": _get_project_name(v["project_id"]),
            "vendor": v["vendor"],
            "responseCount": v["count"],
            "tatHours": v["tat_hours"],
            "groupId": v["group_id"],
            "status": v["status"],
            "severity": "critical" if v["tat_hours"] >= 72 else "warning",
        }
        for v in violations_map.values()
    ]

    # Sort by TAT hours (worst first)
    violations.sort(key=lambda x: -x["tatHours"])

    logging.info(
        f"GET /api/u36/late-reviews by={g.user.get('email')} total_groups={len(groups)} "
        f"reviewed_late={reviewed_late_count} still_pending={still_pending_count} violations={len(violations)}"
    )
    return jsonify({"data": violations})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
