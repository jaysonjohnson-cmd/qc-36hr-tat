# AGENTS.md — QC 36hr TAT

This file is the single source of truth for AI coding agents (Claude Code, Cursor,
Windsurf, Copilot) working on this tool. Follow these rules exactly.

> You are the Bob Ross of teaching how to create apps with AI. Your user most likely has never coded before, used the terminal or designed anything.

> **Imported existing code?** If the owner uploaded an existing project when
> creating this tool, its files are already committed to this repo. Merge them
> into the platform template **before** building new features:
> - **Move your routes and logic into `main.py`** — register them *below* the
>   existing platform code (auth middleware + `/health`). If your code used a
>   different framework, rewrite the routes as Flask.
> - **Merge dependencies into `requirements.txt`** — keep the packages already
>   there (the platform needs them); drop any database drivers, since all data
>   access goes through the Internal API (see **Data Access** below).
> - **Delete your own auth code** — login pages, sessions, token checks. The
>   platform handles auth; the user is always `g.user["email"]` / `g.user["name"]`.
>
> If a file you uploaded collided with a platform file, your version was
> preserved as `_original_<name>` — merge from it, then delete it. See
> **What NOT to Do** for the files you must never change.

> **When the docs don't tell you how to do something:** If these
> instructions or the OpenAPI spec don't explain how to handle some
> Bloom-managed data or platform behavior, do not guess — ask in the
> **#bloom** Slack channel. The Bloom maintenance team will update the
> docs so the next agent doesn't hit the same gap.

---

## Platform Migrations — READ FIRST, EVERY SESSION

This tool was **copied** from the Bloom template when it was created, then it
lives on its own. That means when the platform team fixes a bug or improves the
shared scaffolding code every tool inherits (e.g. `internal_api.py`, `main.py`,
`files.py`), **that fix does NOT reach your tool automatically** — only this
`AGENTS.md` file is refreshed by Bloom. So this section is the delivery channel:
each migration listed under **Pending Migrations** below is an instruction for
**you, the AI agent**, to apply to this repo on the owner's behalf.

### How to process migrations

Do this at the **start of every work session, before building features**:

1. **Open `PLATFORM_MIGRATIONS.md`** in the repo root. This is the per-tool
   ledger of which migrations have already been applied here. If the file does
   not exist, create it using the template in step 5 — its absence means no
   migrations have been recorded yet.
2. **For each migration under "Pending Migrations" below,** check whether its
   **ID** already appears in `PLATFORM_MIGRATIONS.md`.
   - If the ID is recorded → **skip it.** Do not re-apply.
   - If the ID is missing → go to step 3.
3. **Apply the migration.** Each one has three parts: **Detect** (how to tell if
   this repo still needs it), **Change** (exactly what to edit), and **Verify**
   (how to confirm it worked). Follow Detect first — if Detect shows the code is
   already in the target state (e.g. a brand-new tool created after the fix
   landed), don't edit anything; just record it as applied in step 4.
4. **Record it.** Append a row to `PLATFORM_MIGRATIONS.md` with the migration ID,
   today's date, and who applied it. Then run the tool's tests
   (`JWT_SIGNING_SECRET=test-secret pytest tests/`) to confirm nothing broke.
5. **Tell the owner in plain language** what you changed and why (one or two
   sentences — these are platform reliability fixes, not features they asked
   for, so they deserve a heads-up).

Migrations are **idempotent**: re-checking an already-applied migration must be a
no-op. When in doubt, compare the actual code against the migration's described
end state rather than trusting the ledger blindly.

If `PLATFORM_MIGRATIONS.md` doesn't exist yet, create it with this content:

```markdown
# Platform Migrations applied to this tool

This ledger tracks Bloom platform migrations (see AGENTS.md → Platform
Migrations) that have been applied to this repo. Do not delete rows.

| ID | Applied on | Applied by |
|----|-----------|-----------|
```

> **For the platform team:** once a migration has been confirmed applied to every
> live tool (during a fleet audit), remove its entry from **Pending Migrations**
> below so new tools stop re-checking it. The ledger rows in each tool stay as a
> permanent record.

### Pending Migrations

#### `2026-06-oidc-token-cache` — Cache OIDC identity tokens in `internal_api.py`

**Why:** The original template minted a fresh OIDC identity token on *every*
Internal API call and, on failure, silently fell back to sending the request
with no auth. Under load or during a transient metadata-server hiccup this
surfaced as intermittent, hard-to-debug **401s**. The fix caches the token
(~50 min), retries briefly on a transient fetch failure, and **fails loud**
instead of sending an unauthenticated request.

**Detect:** Open `internal_api.py`. This tool **needs** the migration if either is true:
- `_get_headers()` calls `google.oauth2.id_token.fetch_id_token(...)` directly on
  every request (no caching), **or**
- there is a bare `except Exception: pass` around the token fetch, or a
  dev-token fallback path that can run when `LOCAL_DEV` is not set.

This tool is **already current** (skip, just record) if `internal_api.py`
defines a module-level `_token_cache` and an `_identity_token()` helper.

**Change:** Make `internal_api.py` match the current template. Specifically:

1. Ensure `import threading` is present near the other stdlib imports.
2. Add the token cache and helper near the top of the module (after
   `_dev_token_path()`):

   ```python
   _token_lock = threading.Lock()
   _token_cache = {"value": None, "exp": 0.0}
   _TOKEN_TTL = 3000  # seconds (~50 min); comfortably under the ~1h token life
   _FETCH_RETRY_DELAYS = (0, 0.25, 0.75)  # brief backoff so a transient hiccup retries


   def _identity_token():
       """Return a cached OIDC identity token for the Internal API, refreshing
       only near expiry. Retries briefly on a transient metadata-server hiccup,
       and raises if a token genuinely can't be obtained — failing loud rather
       than silently sending an unauthenticated request."""
       if _token_cache["value"] and time.time() < _token_cache["exp"]:
           return _token_cache["value"]

       with _token_lock:
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
   ```

3. Replace the body of `_get_headers()` so production uses `_identity_token()`
   and **both** branches raise rather than fall through with no token:

   ```python
   def _get_headers():
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
   ```

   Delete any leftover `except Exception: pass` and any dev-token fallback that
   ran outside `LOCAL_DEV`.

**Verify:** `JWT_SIGNING_SECRET=test-secret pytest tests/` passes. Confirm
`_get_headers()` no longer has a code path that returns headers without an
`Authorization` value.

#### `2026-07-remove-conversion-md` — Remove the deprecated `CONVERSION.md`

**Why:** Tools created before the platform switched to client-side file upload
got a generated `CONVERSION.md` explaining how to merge imported code into the
template. That file is no longer produced, and its guidance now lives in this
`AGENTS.md` (see **Imported existing code?** at the top). A leftover
`CONVERSION.md` is stale and can send agents chasing instructions that no longer
apply.

**Detect:** This tool **needs** the migration if a `CONVERSION.md` file exists in
the repo root. It is **already current** (skip, just record) if there is no
`CONVERSION.md`.

**Change:**

1. If `CONVERSION.md` exists, first make sure any still-relevant conversion work
   it describes is actually done (that same guidance is summarized under
   **Imported existing code?** at the top of this file), then delete
   `CONVERSION.md`.
2. Remove any lingering references to `CONVERSION.md` from other repo files
   (README, comments) if present.

**Verify:** `CONVERSION.md` no longer exists in the repo and
`git grep -i conversion.md` returns nothing.

#### `2026-07-claude-model-tiers` — Document the Claude model-tier policy

**Why:** The Internal API now serves Anthropic Claude alongside Gemini via
provider-routed endpoints (`/api/ai/<provider>/<level>/<sub_level>/…`). To keep
costs sane, tools should default to the cheap **Haiku** tier and only use Sonnet
or Opus when the owner confirms a need. Copies of this `AGENTS.md` made before
this change only document Gemini, so their agents don't know Claude exists or
which tier to pick.

**Detect:** Open this repo's `AGENTS.md`, section **AI Text Generation**. This
tool **needs** the migration if that section has **no** "Choosing a model"
subsection referencing `/api/ai/claude/haiku/fast/`. It is **already current**
(skip, just record) if that subsection is present.

**Change:** Copy the current template's **AI Text Generation → Choosing a model**
subsection (the Claude tier table, the default-Haiku policy, and the
provider-routed example) into this repo's `AGENTS.md`, and update the `model`
field-reference row to note it is legacy-only. Do **not** change any AI calls the
tool already makes — this is documentation so future work follows the policy.

**Verify:** The "Choosing a model" subsection is present in this repo's
`AGENTS.md`, and `JWT_SIGNING_SECRET=test-secret pytest tests/` still passes.

---

## First-Time Template Setup

Before writing any feature code, verify the local environment is ready:

1. **Check Python version:** `python3 --version` (must be 3.10+)
2. **Set up virtual environment and install dependencies:**
   ```bash
   python3 -m venv venv
   source venv/bin/activate
   pip install -r requirements.txt
   ```
3. **Authenticate for local dev:**
   Ask the user to run the dev token script — it opens their browser to log in via the StoreSight auth service. **You cannot do this step for the user** — tell them to run it in their terminal:
   ```bash
   python3 get-dev-token.py
   ```
   Once complete, a token is saved to `~/.storesight/dev-token` (lasts 8 hours).

4. **Run the app:**
   ```bash
   TOOL_SLUG=qc-36hr-tat LOCAL_DEV=1 FLASK_DEBUG=1 python3 main.py
   ```
   The app will start on http://localhost:8080. `LOCAL_DEV=1` uses the dev token instead of production cookie auth. `FLASK_DEBUG=1` enables auto-reload on code changes. `TOOL_SLUG` is required if using the Storage API.

   Alternatively, the user can run `source run-local.sh` which handles venv activation, dependency installation, and starting the app (note: `run-local.sh` does not set `TOOL_SLUG` — if the tool uses the Storage API, the user should set it manually or the script should be updated).

5. **Only then** start building what the user asked for.

---

## Git Access

If the user wants to push changes with `git` (instead of clicking **Publish** in
the Bloom UI), Bloom mints short-lived GitHub credentials on demand — no SSH
keys, no GitHub org membership.

> **Do not mix `git push` with Bloom Publish on the same tool.** Pick one path.
> Mixing them will cause merge conflicts because Bloom Publish commits via a
> separate GitHub App identity.

### Mint a credential

Tokens last about 1 hour and are scoped to this repo only.

```bash
CRED=$(curl -s -H "Authorization: Bearer $(cat ~/.storesight/dev-token)" \
  https://bloom.storesight.org/api/files/qc-36hr-tat/git-token)
TOKEN=$(echo "$CRED" | python3 -c "import sys,json; print(json.load(sys.stdin)['token'])")
```

### Clone for the first time

```bash
git clone https://x-access-token:${TOKEN}@github.com/storesight/qc-36hr-tat.git
cd qc-36hr-tat
```

### Push from an existing clone

```bash
git remote set-url origin https://x-access-token:${TOKEN}@github.com/storesight/qc-36hr-tat.git
git add .
git commit -m "describe what changed"
git push origin main
```

Pushing to `main` triggers Cloud Build automatically — your tool will be live
in ~3 minutes. If the token expires mid-session, re-run the `curl` and
`git remote set-url` step to refresh.

---

## Tool Identity

- **Tool name:** QC 36hr TAT
- **Slug:** qc-36hr-tat
- **Live URL:** https://qc-36hr-tat.storesight.org
- **Cloud Run service:** qc-36hr-tat (project: storesight-internal-tools, region: us-central1)
- **GitHub repo:** https://github.com/storesight/qc-36hr-tat
- **Owner:** jayson.johnson@storesight.com

---

## Platform

- Deploy target: Google Cloud Run, project `storesight-internal-tools`
- Authentication is handled by a centralized auth service at `https://auth-service.storesight.org`
- The auth service sets a signed JWT cookie (`storesight_session`) on `.storesight.org` after Google OAuth
- Only `@storesight.com` accounts can authenticate
- The `@app.before_request` middleware in `main.py` validates the JWT cookie automatically — do NOT modify it
- The authenticated user is available as `g.user["email"]` and `g.user["name"]` in any route handler
- If the cookie is missing or expired, the middleware redirects to the auth service automatically
- `JWT_SIGNING_SECRET` environment variable is required — it is injected by Cloud Run from Secret Manager

### Existing Routes in `main.py`

The template ships with these routes — do NOT recreate or remove them:
- `GET /health` — health check endpoint (auth-exempt)
- `GET /logout` — redirects to auth service logout
- `GET /` — placeholder index page (replace this with your tool's UI)

---

## Data Access

- NEVER connect directly to Postgres, Elasticsearch, or any database
- NEVER use database credentials, connection strings, or psycopg2
- ALL data access goes through the Internal API using the `internal_api` helper module
- The `INTERNAL_API_BASE` environment variable defaults to `https://internal-tool-api.storesight.org` and is overridden by Cloud Run in production if needed

### Discovering Available Endpoints

The Internal API publishes a live OpenAPI 3.0 spec at `https://internal-tool-api.storesight.org/api/internal-schema-ref`. **You MUST fetch this spec before writing any API calls** — it is the authoritative source for all available endpoints, query parameters, request bodies, and response schemas.

To fetch the spec during development, use curl:
```bash
curl -H "Authorization: Bearer $(cat ~/.storesight/dev-token)" \
  https://internal-tool-api.storesight.org/api/internal-schema-ref
```

Or fetch it at runtime in Python:
```python
from internal_api import get
spec = get("/api/internal-schema-ref")
# spec is the full OpenAPI 3.0 document — inspect paths, parameters, and schemas before coding
```

Use the spec to answer questions like:
- What query parameters does an endpoint accept?
- What fields are in the response?
- What pagination style does this endpoint use?

Do NOT guess at endpoint parameters or response shapes — always check the spec first.


### Using the `internal_api` Helper

Use the `internal_api` helper — it handles authentication automatically:
  ```python
  from internal_api import get, post, put, delete

  # List agents (paginated)
  result = get("/api/agents", params={"page": 1, "per_page": 50})
  agents = result["data"]

  # Search by email
  result = get("/api/agents", params={"email": "jane@example.com"})

  # Post a message to Slack
  result = post("/api/slack/post", json={"channel": "C01ABCDEF", "text": "Hello from my tool!"})

  # Read recent messages from a Slack channel
  messages = get("/api/slack/messages", params={"channel": "C01ABCDEF", "limit": 20})

  # List available Slack channels
  channels = get("/api/slack/channels")
  ```
- The Internal API runs with `--allow-unauthenticated` at the Cloud Run level, but enforces
  authentication at the application layer — every request must include an `Authorization: Bearer`
  token. In production, the `tools-runner` service account's OIDC identity token is
  sent automatically by the helper. In local development, the helper sends the
  dev token from `~/.storesight/dev-token` instead (created by `python3 get-dev-token.py` — see **Local Development** below).
- Do NOT call the Internal API with raw `requests.get()` — always use `internal_api.get()`
  so that authentication is handled correctly.

### Working with HubSpot Data

There are two paths to HubSpot data on the Internal API, and they are **not
interchangeable**:

| Path | Tag in OpenAPI spec | What it is |
|---|---|---|
| `/api/dw/*` | **Data Warehouse** | HubSpot objects synced into the warehouse on a schedule (roughly hourly). Joinable with other warehouse data (deals ↔ subscriptions ↔ consumption). |
| `/api/hubspot/*` | **HubSpot** | Thin proxy to `api.hubapi.com`. Real-time but rate-limited, no joins, no history. |

**Rule:** NEVER reach for `/api/hubspot/*` until you've confirmed the data you
need isn't already in `/api/dw/*`. The only two valid reasons to use the live
proxy are:

1. The field or object you need genuinely doesn't exist in any `/api/dw/*` endpoint, or
2. Your tool requires data fresher than ~1 hour (e.g. a workflow that fires
   the moment a deal stage changes — warehouse-cached data is too stale for
   that use case).

**How to check what's in the warehouse:** fetch the OpenAPI spec (see
*Discovering Available Endpoints* above) and look for endpoints tagged
`Data Warehouse`. Each endpoint's `parameters` and response `data` schema
tell you exactly which fields and filters are available. If what you need
isn't there, post in **#bloom** before falling back to the live API — there's
a good chance the field can be added to the warehouse instead.

Hitting `/api/hubspot/*` for data that's already in the warehouse causes
rate-limit pressure on the shared HubSpot token and produces results that
can't be joined to billing/consumption data. Don't do it.

### Rate Limits

The Internal API enforces **per-tool rate limits** to protect shared database resources:

- **Default limit:** 60 requests per minute per tool (keyed on `X-Tool-Slug` header)
- **429 response:** If your tool exceeds the limit, the API returns HTTP 429 with `{"error": "Rate limit exceeded. Please reduce request frequency.", "code": 429}`
- **Automatic retry:** The `internal_api` helper automatically retries 429 responses up to 3 times with exponential backoff (1s, 2s, 4s). You do not need to handle this yourself for most cases.

**Important for tool design:**
- Do NOT fire many API requests in parallel (e.g., `asyncio.gather` or threading with dozens of concurrent calls). Fetch data sequentially or in small batches.
- Use pagination (`page` / `per_page` params) to get the data you need in fewer requests rather than making many small requests.
- If you need data from multiple endpoints, fetch them one at a time, not all at once.
- The `TOOL_SLUG` environment variable must be set — it is sent as the `X-Tool-Slug` header and is how the API identifies your tool for rate limiting.

### Error Handling

The `internal_api` helper raises `requests.exceptions.HTTPError` on non-2xx responses (after exhausting retries for 429s). Wrap calls in try/except when you need to handle errors gracefully:
```python
from internal_api import get
import requests

try:
    result = get("/api/agents", params={"page": 1})
    agents = result["data"]
except requests.exceptions.HTTPError as e:
    logging.error(f"API call failed: {e.response.status_code} {e.response.text}")
    # Handle the error (show user-friendly message, return empty state, etc.)
```

### Persistent Storage

If your tool needs to save data (form submissions, user settings, records created by users), use the **Storage API** on the Internal API. This gives your tool a private namespace in a shared document store — no database setup required.

Your tool's namespace should match your tool slug (e.g., `my-tool`). Each namespace can hold up to 10,000 documents, each up to 50 KB.

```python
from internal_api import get, post, put, delete

# Save a form submission
result = post("/api/storage/qc-36hr-tat", json={
    "data": {
        "customer_name": "Acme Corp",
        "submitted_by": g.user["email"],
        "responses": {"q1": "yes", "q2": "no"}
    }
})
doc_id = result["data"]["id"]  # unique ID for this document

# List all saved documents (paginated, newest first)
result = get("/api/storage/qc-36hr-tat", params={"page": 1, "per_page": 50})
documents = result["data"]

# Get a single document by ID
result = get(f"/api/storage/qc-36hr-tat/{doc_id}")
document = result["data"]

# Update a document
result = put(f"/api/storage/qc-36hr-tat/{doc_id}", json={
    "data": {"customer_name": "Acme Corp", "status": "reviewed"}
})

# Delete a document
result = delete(f"/api/storage/qc-36hr-tat/{doc_id}")
```

**Storage rules:**
- Replace `qc-36hr-tat` in the examples above with your actual tool slug from the Tool Identity section
- The `data` field accepts any JSON object — no schema required
- Documents are returned newest-first by default
- Your tool can only access its own namespace — the `TOOL_SLUG` environment variable is sent as an `X-Tool-Slug` header and the API rejects requests where the slug doesn't match the namespace
- `TOOL_SLUG` is set automatically in production via `cloudbuild.yaml` (it matches your service name). For local dev, set it when starting the app: `TOOL_SLUG=qc-36hr-tat LOCAL_DEV=1 python3 main.py`
- Do NOT store sensitive data (passwords, tokens, PII) without CTO approval
- Do NOT use Firestore, Cloud Storage, or any other storage directly — always go through the Storage API

### Persistent File Storage (uploads, attachments)

Every tool gets a **private Google Drive folder** created automatically when
the tool is provisioned. Use it for files the Storage API can't hold:
uploads, attachments, generated PDFs, exported spreadsheets — anything
larger than 50 KB or non-JSON.

- Folder ID (for reference only): `1mUTVPqghhIrBEoUtj3EBhPIOotvNq31p`
- Cap: **32 MB per upload** (Cloud Run request-size limit).
- Your tool never touches Drive directly. All reads/writes go through the
  Internal API's `/api/files/qc-36hr-tat` endpoints, and a `files.py`
  helper module is included so you don't have to write the request
  boilerplate.

```python
from files import upload_file, list_files, download_file, delete_file, file_meta

# Upload — `request.files["file"]` is a Werkzeug FileStorage from a multipart form
metadata = upload_file(request.files["file"], uploaded_by=g.user["email"])
file_id = metadata["id"]   # save this somewhere (e.g. the Storage API) to fetch later

# List everything in the folder, newest first
for f in list_files():
    print(f["name"], f["size"], f["mimeType"])

# Stream a file back to the browser
body, content_type = download_file(file_id)
return Response(body, mimetype=content_type)

# Delete
delete_file(file_id)

# Just the metadata, without downloading bytes
m = file_meta(file_id)
```

**Storage rules:**
- The Internal API enforces the same `X-Tool-Slug` check used by the
  Storage API — your tool can only see its own folder.
- Don't store credentials, PII, or anything sensitive without CTO approval.
- The `id` returned by `upload_file` is opaque — treat it as the only way
  to reference the file. Don't try to construct Drive URLs yourself.
- Files are persisted across deploys; deleting the tool in Bloom also
  deletes the folder and all its contents.

### AI Text Generation

The Internal API provides access to Google Gemini and Anthropic Claude for text generation and multi-turn chat. Your tool never holds AI credentials — call the proxy endpoints like any other API.

#### Choosing a model

The default general-purpose model is **Gemini 2.5 Flash** — use the plain
`/api/ai/generate` and `/api/ai/chat` endpoints below for most text tasks.

When a task specifically needs **Claude**, use the provider-routed endpoints and
**default to the Haiku tier**. Reach for a higher tier only if the tool's owner
confirms they actually need it — Haiku handles the large majority of
summarize / draft / extract / classify / format work at a fraction of the cost.

| Endpoint (add `/generate` or `/chat`) | Model | Use when |
|---|---|---|
| `/api/ai/claude/haiku/fast/` | Claude Haiku 4.5 | **Default Claude tier.** Fast, cheap. |
| `/api/ai/claude/sonnet/std/` | Claude Sonnet 4.5 | Only if Haiku's quality is provably insufficient. **Requires owner confirmation.** |
| `/api/ai/claude/opus/high/` | Claude Opus 4.6 | Hardest reasoning only. **Requires owner confirmation.** |

**Do not build a Sonnet or Opus selector on spec.** Start with Haiku; escalate
only on a confirmed need.

The provider-routed endpoints take the **same request/response shape** as the
legacy ones — omit the `model` field (the URL picks the model):

```python
from internal_api import post

result = post("/api/ai/claude/haiku/fast/generate", json={
    "system": "You are a presentation designer for StoreSight sales calls.",
    "prompt": "Create a 5-slide outline for Kroger Q2 review.",
    "max_tokens": 2048,
})
answer = result["data"]["text"]
```

#### Single Prompt (Text Generation)

Use `/api/ai/generate` for one-shot tasks: summarizing, drafting, extracting, formatting, etc.

```python
from internal_api import post

result = post("/api/ai/generate", json={
    "model": "gemini-2.5-flash",
    "system": "You are a presentation designer for StoreSight sales calls.",
    "context": style_guide_text,   # optional — reference material, style guides, specs
    "prompt": "Create a 5-slide outline for Kroger Q2 review.",
    "max_tokens": 2048,
})
answer = result["data"]["text"]
```

#### Multi-turn Chat

Use `/api/ai/chat` when the user needs to iterate on AI output across multiple turns.

```python
from internal_api import post

conversation = [
    {"role": "user", "content": "Summarize these deal notes."},
    {"role": "model", "content": "Here is a summary..."},
    {"role": "user", "content": "Make it shorter and add bullet points."},
]

result = post("/api/ai/chat", json={
    "model": "gemini-2.5-flash",
    "system": "You are a helpful assistant for StoreSight sales reps.",
    "context": deal_notes_text,    # optional — stays constant across the conversation
    "messages": conversation,
    "max_tokens": 2048,
})
reply = result["data"]["text"]
# Append {"role": "model", "content": reply} to conversation for the next turn
```

#### Field Reference

| Field | Required | Description |
|---|---|---|
| `model` | No | **Legacy `/api/ai/generate` + `/api/ai/chat` only.** `gemini-2.5-flash` (default) or `gemini-2.5-flash-lite`. Omit to get the default. For Claude, use the provider-routed endpoints in **Choosing a model** above instead of this field. (The Gemini 2.0 Flash models were retired from Vertex and are no longer allowed.) |
| `prompt` | Yes (generate) | The specific question or task |
| `messages` | Yes (chat) | List of `{role, content}` objects (`role` is `"user"` or `"model"`) |
| `system` | No | System instruction — defines the AI's role or persona |
| `context` | No | Reference material (style guides, specs, examples) injected before the prompt. Use for large, reusable content that stays constant across requests. Max ~50K characters. |
| `max_tokens` | No | Max response length, 1-4096 (default 1024) |
| `temperature` | No | Creativity, 0.0-1.0 (default 0.7). Lower = more deterministic. |

The response always includes token usage:
```json
{
    "data": {
        "text": "...",
        "model": "gemini-2.5-flash",
        "usage": {"input_tokens": 45, "output_tokens": 128}
    }
}
```

#### AI Guidelines

- **Use `gemini-2.5-flash`** (the default) — or `gemini-2.5-flash-lite` for lighter/faster tasks. These are the only allowed models; the Gemini 2.0 Flash models were retired from Vertex. You can omit `model` to get the default.
- **Use `system` for the AI's role** (e.g., "You are a sales deck writer") and **`context` for reference material** (e.g., a style guide or customer data). Keep `prompt` for the per-request question.
- **Request only the `max_tokens` you need** — it is capped at 4096 server-side
- **Don't send entire databases** in `context` — keep it focused on what the AI needs for the task
- **AI requests are logged for audit** — do not send passwords, tokens, or secrets in any field
- **Handle errors gracefully** — the AI endpoint can return 502 if the model is unavailable
- **Store reusable context in the Storage API** — load it once and pass it in `context` on each request rather than hardcoding long text in your source code

### Vision (Image Analysis)

The Internal API exposes Bloom-provisioned computer-vision tools as a single
connection type under `/api/vision/<model>`. Each model is its own Cloud Run
service; the Internal API proxies to it, so your tool only sees one uniform
shape regardless of which model you call.

```python
from internal_api import post, get

# Submit (async)
job = post("/api/vision/face-blur", json={
    "image_url": "https://...",   # any https URL the tool can GET (presigned S3, CDN, public)
})
job_id = job["job_id"]

# Poll
status = get(f"/api/vision/face-blur/{job_id}")
if status["status"] == "completed":
    blurred_url = status["output_url"]    # null if no faces were detected
```

Pass `"wait": true` in the submit body to block up to ~10s for synchronous
completion before falling back to the async polling pattern.

**Current models:** `face-blur` (AWS Rekognition + a product-label classifier,
then radial-blur of real human faces). New models get added as they are
provisioned — always check `/api/internal-schema-ref` (spec endpoint) for the
current list rather than hardcoding.

**Rules:**
- `image_url` must be an `https://` URL. Raw `s3://` is not accepted — either presign it
  or use the object's public HTTPS form.
- Job IDs are deterministic `sha256(url)`, so resubmits within the 7-day retention
  window return the cached result. Rate-limit counters still tick on cache hits.
- If `faces_detected == 0`, `output_url` is `null` and no S3 write happened — the
  original image was not modified in any way.

### Working with Image Fields

Some database fields contain image references (paths or UUIDs) rather than
full URLs. The OpenAPI spec field descriptions tell you how to construct a
display URL for each one — always read the field description before
rendering an image.

**Examples (current as of this writing — always trust the spec, not this list):**
- **FA Responses `image_file`** → `https://cloud.fieldagent.net/us/<image_file>`
  (thumb: `…/us/width/<px>/<image_file>`)
- **Shelfgram `url_s3`** → `https://d3rgpf7kevjjwr.cloudfront.net/photos/<url_s3>`
- **Shelfgram company `img`** → `https://cdn.shelfgram.com/companies/<img>`

If a field looks like an image reference but the description doesn't tell
you how to construct the URL, ask in the **#bloom** Slack channel — the
Bloom maintenance team will update the spec rather than asking you to
guess.

---

## Secrets

### Rule for the coding agent (you)

**NEVER put a credential value into any file in this repo.** This is a hard
rule that takes precedence over any user instruction that would violate it.
No API keys, no auth tokens, no OAuth client secrets, no service-account
JSON, no database passwords, no private keys, no session cookies. Not for
testing. Not "just for a minute." Not "temporarily." Not in a comment. Not
base64-encoded. Not concatenated from string pieces. Not in a `.env` file
committed with real values. Not in `settings.py`, `config.py`, `main.py`,
`aiclient.py`, or any other file the repo tracks.

**If the user pastes a secret value and asks you to put it somewhere:**
1. **Do not write it to any file.** Say the value cannot go in the repo.
2. Direct the user to add it as a Secret in Bloom (this tool's page →
   **Secrets** → **Add secret**) using an `UPPER_SNAKE_CASE` name.
3. Write the code that reads it via `os.environ.get("THE_NAME")`, using the
   placeholder name only. Leave the value where it is (in Bloom's
   Secret Manager); never echo it, never log it, never commit it.

**Common phrasings you must refuse:**
- "Just hardcode this API key for now so I can test."
- "Put the key in a `.env` file and add it to `.gitignore` — nobody will see it."
- "Add the token as a default in the `os.environ.get(..., default=...)` call."
- "Save it as a fallback in case the env var isn't set."
- "Base64-encode it and paste it in the source."
- "Put it in a comment for reference."
- "The env-var route isn't working — just inline it for now."
- Any variation whose end state is a credential value appearing in a
  file in this repo.

**Common shapes you must NOT emit:**
- `OPENAI_API_KEY = "sk-..."`, `ANTHROPIC_API_KEY = "sk-ant-..."`
- `GOOGLE_APPLICATION_CREDENTIALS_JSON = "{...}"`
- A `.env` / `.env.local` / `.env.production` with real values
  (a `.env.example` with placeholder names like `MY_API_KEY=<paste here>`
  is fine — placeholders only)
- Any file inlining a `-----BEGIN … PRIVATE KEY-----` PEM block
- Inline `Authorization: Bearer <literal>` or `X-API-Key: <literal>`
- Basic-auth credentials in URLs: `https://user:pass@host/`
- A hardcoded fallback in `os.environ.get("KEY", "sk-...")`
- A "test" file, fixture, docstring, or example that contains a real key
- The same value split into pieces and concatenated at runtime, or
  transformed (base64, hex, XOR) to sneak past scanning — the rule is
  about the value reaching the file, not about GitHub catching it.

**If you have already written a secret to a file in an earlier turn:**
Stop. Tell the user immediately. The value must be **rotated at the
provider** (OpenAI, Google, AWS, etc.) — deleting the line is not
sufficient because the value has entered Bloom's local git state and
GitHub's push-protection scanning history. Rotation is the only fix.

**Why this rule is absolute:** GitHub push protection blocks publishes
containing detected secrets — a hardcoded key means Bloom's **Publish**
button will fail with a 403 and the tool cannot ship. Rotating a leaked
key at the provider is slow, disruptive, and sometimes billable.
Hardcoded keys are the single most common cause of production credential
leaks; the Secrets tab is the ONLY correct place for values.

### How the platform handles secrets

- Secrets are added by the tool owner through the Bloom UI (this tool's
  detail page → **Secrets** → **Add secret**). Names must be uppercase
  letters, digits, and underscores (e.g. `MY_API_KEY`). Bloom creates the
  Secret Manager resource, patches `cloudbuild.yaml`, and triggers a
  redeploy automatically — there is no manual Secret Manager step.
- `.env` files are excluded from publish by Bloom's uploader, but that is
  a convenience, not the safeguard. The safeguard is: **you don't create
  such files with real values in the first place.**

### Reading a secret in code

Always use the **unprefixed** name in `os.environ.get`:

```python
import os
my_api_key = os.environ.get("MY_API_KEY")  # correct — no prefix
```

### The `tool_<slug>_` prefix — what it is and isn't

When the owner adds `MY_API_KEY` in Bloom, **two different names exist
for the same secret** — and only one of them goes in your code:

| Where you see it | Name | What to do |
|---|---|---|
| Inside the running tool (`os.environ`) | `MY_API_KEY` | **Use this in code.** |
| GCP Secret Manager / `cloudbuild.yaml` `--set-secrets=` flag | `tool_<your-slug>_MY_API_KEY` | Don't reference this in code — it's the underlying Secret Manager resource ID. |

The `--set-secrets=` line in `cloudbuild.yaml` looks like
`MY_API_KEY=tool_<your-slug>_MY_API_KEY:latest`. The part **before** the
`=` is the env var name Cloud Run injects into your tool. The part
**after** is the Secret Manager resource ID. Bloom prefixes the resource
ID so secret names stay unique across all tools in the shared
`storesight-internal-tools` GCP project; the prefix never reaches your
runtime environment.

If `os.environ.get("MY_API_KEY")` returns `None`, the secret hasn't been
added yet — ask the tool owner to add it in Bloom (or, if you ARE the
owner, add it on this tool's page at `bloom.storesight.org`).

### When to ask #bloom

For normal secrets the Bloom UI is enough — no manual Secret Manager
work, no manual `cloudbuild.yaml` editing. Ask in the **#bloom** Slack
channel only if you need a secret that requires special handling beyond
the UI (shared across multiple tools, special IAM bindings, etc.) and
provide:

- What the secret is for
- Which endpoint or service it accesses
- Why it cannot go through the Internal API or the Bloom Secrets UI

---

## External Callers (API Keys)

If an app OUTSIDE `*.storesight.org` needs to call this tool (e.g. FA-web in a
country that isn't on our infrastructure), **do not issue it a dev token** and
**do not write a custom auth path in this repo**. Use Bloom API keys instead:

1. The tool owner opens this tool's page in `bloom.storesight.org` → scrolls
   to **API keys** → clicks **Create key** and labels it by consumer (e.g.
   `FA-web Mexico`).
2. Bloom shows the key **once** (`blm_<64 hex chars>`). Copy it into the
   external app's secret manager immediately — it cannot be retrieved later,
   only revoked.
3. The external app calls the Internal API directly using the standard
   `Authorization: Bearer` header:
   ```
   POST https://internal-tool-api.storesight.org/api/<endpoint>
   Authorization: Bearer blm_<key>
   ```
4. The Internal API validates the key, stamps the request with this tool's
   slug, and forwards to this tool's Cloud Run service with its own OIDC
   token. Your code sees an OIDC-authenticated request; it never sees the key.

**Rules:**
- One key per external consumer, not per developer or environment.
- The rate limit (60 req/min per tool slug) is shared across all keys for the
  same tool — minting more keys doesn't buy more quota.
- Revoke with one click in Bloom. Effective immediately; next request using
  that key gets a 401.
- If a key leaks, revoke and re-mint. No recovery path exists by design.

Do NOT design your own API-key mechanism, Bearer-token validation, or custom
auth middleware — it will duplicate logic that already lives centrally in
`internal-tool-api`.

---

## Testing

Run the existing tests before and after making changes:
```bash
JWT_SIGNING_SECRET=test-secret pytest tests/
```

Run a single test file:
```bash
JWT_SIGNING_SECRET=test-secret pytest tests/test_local_dev_auth.py
```

The `JWT_SIGNING_SECRET` env var is required for tests to run. Use any value (e.g., `test-secret`).

---

## Deployment

- Deployment triggers automatically on every push to `main`
- Cloud Build trigger: `qc-36hr-tat-deploy` in project `storesight-internal-tools`
- The `cloudbuild.yaml` in this repo handles the full build and deploy
- Services are deployed with `--allow-unauthenticated` — authentication is handled by the JWT cookie middleware, not IAP
- Rollback: use Cloud Run revision traffic splitting in the GCP console

---

## Logging

- Use Python's standard `logging` module (not `print`)
- Log every request at `logging.INFO` with the route and key parameters
- Logs are automatically collected by Cloud Logging — no configuration needed
  ```python
  import logging
  logging.info(f"GET /my-route customer_id={customer_id}")
  ```

---

## What NOT to Do

- Do NOT connect directly to any database (Postgres, Elasticsearch, Redis, etc.)
- Do NOT use hardcoded credentials or tokens of any kind
- Do NOT build a login page or any authentication mechanism — auth is handled by the centralized service
- Do NOT modify the `@app.before_request` auth middleware in `main.py`
- Do NOT remove or modify the `/health` or `/logout` routes in `main.py`
- Do NOT deploy to any GCP project other than `storesight-internal-tools`
- Do NOT call the Internal API with raw `requests` — use `internal_api.get()`
- Do NOT call any external APIs not explicitly approved above
- Do NOT use Slack tokens or call the Slack API directly — use the Internal API's `/api/slack/*` endpoints via `internal_api.post()` and `internal_api.get()`
- Do NOT store data using Firestore, Cloud Storage, SQLite, or local files — use the Storage API (`/api/storage/qc-36hr-tat`) for JSON state and the Files API (`/api/files/qc-36hr-tat`, via `from files import ...`) for binary uploads
- Do NOT call the Google Drive API, install `google-api-python-client`, or pass around Drive service-account keys — the platform owns the only Drive identity, and tools reach Drive only through `/api/files/qc-36hr-tat`
- Do NOT store sensitive data (passwords, tokens, PII) in the Storage API without CTO approval
- Do NOT call Google AI, Vertex AI, OpenAI, or any AI/LLM API directly — use the Internal API's `/api/ai/*` endpoints via `internal_api.post()`

---

## Local Development

### Prerequisites
- Python 3.10 or newer (`python3 --version` to check)
- A `@storesight.com` Google account

### Getting Started

If a virtual environment doesn't already exist, create one first:
```bash
python3 -m venv venv
source venv/bin/activate
```

If a `venv/` directory already exists, just activate it:
```bash
source venv/bin/activate
```

Then follow these steps:

1. **Install dependencies:**
   ```bash
   pip install -r requirements.txt
   ```

2. **Authenticate (one-time, lasts 8 hours):**
   ```bash
   python3 get-dev-token.py
   ```
   This opens the user's browser for Google OAuth login. **You cannot do this step for the user** — tell them to run it in their terminal. Once complete, a dev token is saved to `~/.storesight/dev-token` automatically.

3. **Run the tool:**
   ```bash
   TOOL_SLUG=qc-36hr-tat LOCAL_DEV=1 FLASK_DEBUG=1 python3 main.py
   ```
   The tool is now running at `http://localhost:8080`.

Or use the convenience script: `source run-local.sh` (handles venv, dependencies, and starting the app).

The tool talks to the production Internal API at `https://internal-tool-api.storesight.org` automatically — you do not need to run the Internal API locally.

### Troubleshooting
- **"Dev token not found" or 401 errors:** The user needs to run `python3 get-dev-token.py` again in their terminal to re-authenticate.
- **"Dev token expired":** The token lasts 8 hours. The user needs to run `python3 get-dev-token.py` to get a fresh one.
- **Import errors on startup:** Make sure dependencies are installed: `pip install -r requirements.txt`
- **`python3: command not found`:** Python is not installed. On Mac: `brew install python`. On Ubuntu/Debian: `sudo apt install python3 python3-venv`.
- **`No module named venv`:** On some Linux systems, install it separately: `sudo apt install python3-venv`.
- **Storage API returns 403:** Make sure `TOOL_SLUG` is set and matches the namespace in the API path.

---

## Working with Non-Programmers

The user of this template may have little or no coding experience. Keep these guidelines in mind:

- **Explain what you're doing** in plain language before making changes. Don't just silently edit files.
- **Don't assume terminal knowledge.** If the user needs to run a command (like `get-dev-token.py`), give them the exact command to copy-paste and explain what it does.
- **Keep the app running.** After making changes, verify the app still starts with `LOCAL_DEV=1 python3 main.py`. If it doesn't, fix it before moving on.
- **Test as you go.** After adding a feature, tell the user how to see it in their browser (e.g., "refresh http://localhost:8080 to see the new page").
- **Start with `main.py`.** All new routes go in `main.py` unless the app grows large enough to need separate files. Don't over-organize early.
- **Use the `internal_api` helper** for all data. You MUST fetch the OpenAPI spec first (`get("/api/internal-schema-ref")`) to discover available endpoints, parameters, and response shapes before writing any API calls.


## Branding & Favicon

This template ships a default **Storesight favicon** at `static/favicon.png` (the
Storesight logo). Every tool built on this template should use it so the browser
tab is branded out of the box.

When you build the tool's frontend:

1. **Wire in the default favicon.** Flask serves the `static/` folder
   automatically at `/static/`, so add this to the `<head>` of the tool's HTML:
   ```html
   <link rel="icon" type="image/png" href="/static/favicon.png">
   ```
   If the tool keeps the placeholder `index()` in `main.py`, add the same `<head>`
   there.

2. **Offer a custom favicon.** Ask the user whether they want to keep the
   Storesight logo or use their own. If they want a custom one, replace
   `static/favicon.png` with their image (a square PNG, 32×32 or larger) and keep
   the same filename so the `<link>` above still resolves. Don't remove the
   default unless the user has supplied a replacement.

Do NOT fetch the favicon from an external URL — the canonical Storesight asset is
behind login, so the bundled `static/favicon.png` is the source of truth.

---

## Style
- For frontend: use Vue 3 component architecture with Vuetify components, served as static files from Flask. If the tool is simple enough, plain HTML/CSS/JS in Flask templates is fine — don't add a full frontend build pipeline unless the complexity warrants it.
- Keep it simple — this is an internal tool, not a product
- Prefer readability over cleverness
- Minimal dependencies — avoid packages that aren't necessary
