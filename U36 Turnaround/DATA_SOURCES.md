# U36 Turnaround — Data Sources & Probing Guide

A starter map for anyone who wants to poke at the **36-Hour Review Turnaround (U36)**
KPI. Everything the report pulls comes from **one** Postgres database. No passwords
live in this file — grab creds from the `.env` (see below) or the vault.

> **KPI in one line:** on-time = a response group's *first review* happened within
> **36 hours** of its submission. Target is **90%**. Population excludes test,
> screener, and ticket jobs.

---

## 1. The database

| | |
|---|---|
| **System** | `fieldagent_us` (FieldAgent US production Postgres) |
| **Engine** | PostgreSQL |
| **Access** | Read-only queries. Don't write. |

Credentials live in the vault / Secrets `.env` — grab them there, don't hardcode.

There's a stashed helper — **`fa_u36_turnaround`** — that reproduces the whole
KPI and slices it a dozen ways. It expects `FA_CONN_STRING` in the environment.
`find_script fa_u36_turnaround` → `run_script`. Reuse it before rebuilding anything.

---

## 2. Tables you'll actually touch

Only three relations do the heavy lifting.

### `job_jobresponsegroup` (alias `g`) — the star of the show
The grain of the KPI: one row per submitted response group.

| Column | Meaning / use |
|---|---|
| `job_id` | FK → `job.id` |
| `"submissionDateTime"` | When it was submitted (camelCase → **must be quoted**). The clock starts here. |
| `first_review_ts` | Timestamp of first review. `NULL` = still pending. The clock stops here. |
| `tp_review_company` | Third-party review vendor code (e.g. `CF`, `RC`). `NULL`/`''` = internal FA review. |

>  **`first_review_minutes` is unreliable — do not use it.** Always compute
> turnaround from the raw timestamps.

### `job` (alias `j`) — population filters
| Column | Meaning / use |
|---|---|
| `id` | PK, joined from `g.job_id` |
| `"isTestJob"` | Exclude `= true` (quoted, camelCase) |
| `is_screener_job` | Exclude `= true` |
| `is_ticket_job` | Exclude `= true` |
| `project_id` | FK → `project_project.id` |

### `project_project` (alias `pp`) — program labels
| Column | Meaning / use |
|---|---|
| `id` | PK, joined from `j.project_id` |
| `title` | Free-text project name. `program_id` is unpopulated, so programs are
  derived by **normalizing `title`** with regex (see report's `PROG_SQL`). |

---

## 3. The canonical WHERE clause

Every KPI query starts from the same join + filter. Copy/paste this and build on it:

```sql
FROM job_jobresponsegroup g
JOIN job j ON j.id = g.job_id
WHERE j."isTestJob" = false
  AND j.is_screener_job = false
  AND j.is_ticket_job   = false
```

On-time predicate (the whole ballgame):

```sql
(g.first_review_ts IS NOT NULL
 AND g.first_review_ts <= g."submissionDateTime" + interval '36 hours')
```

Hello-world query — monthly on-time %:

```sql
SELECT date_trunc('month', g."submissionDateTime")::date AS mon,
       count(*) AS subs,
       round(100.0 * avg((g.first_review_ts IS NOT NULL
         AND g.first_review_ts <= g."submissionDateTime" + interval '36 hours')::int), 1) AS u36_pct
FROM job_jobresponsegroup g
JOIN job j ON j.id = g.job_id
WHERE j."isTestJob" = false AND j.is_screener_job = false AND j.is_ticket_job = false
  AND g."submissionDateTime" >= '2026-01-01'
GROUP BY 1 ORDER BY 1;
```

---

## 4. Slices the report already computes

Each maps to a labelled SQL block in `u36_turnaround_report.Rmd`. Steal them.

| Report var | What it answers |
|---|---|
| `D_MONTH` | Monthly on-time % (2026) |
| `D_DAILY` | Daily trend, last 75 days; splits misses into reviewed-late vs still-pending |
| `D_DOW` | On-time % by submission **day of week** (Central TZ) — weekends are the killer |
| `D_HOUR` | On-time % by submission **hour** (Central) |
| `D_DH` | Day × hour heatmap (the Fri-afternoon / Sat-midday danger window) |
| `D_PROG` | Programs by breach **consistency** (weeks below 90% / weeks active) |
| `D_PROGVOL` | Top programs by **miss volume** |
| `D_VENDOR` | Internal vs third-party review path (`tp_review_company`) |
| `D_SEV` | Severity buckets — how late are the misses? (most are ≤12h over) |
| `D_BACKLOG` | Live pending queue (submitted last 4 days, not yet reviewed) |

---

## 5. Gotchas for the next probe-er

- **Quote the camelCase columns:** `"submissionDateTime"`, `"isTestJob"`. Unquoted,
  Postgres folds them to lowercase and errors out.
- **Timezone:** timing views convert to `America/Chicago` (US/Central). Raw
  timestamps are stored in UTC — convert before bucketing by day/hour.
- **`integer64` from RPostgres** breaks `scales::comma`/ggplot — coerce bigints to
  numeric (the report's `fix64()` helper). Only relevant in R.
- **Programs are regex-derived** from `project_project.title`, not a clean FK.
  Expect fuzziness; refine `PROG_SQL` if a new campaign naming scheme shows up.
- **`first_review_minutes` is a trap.** Timestamps only.
- Last ~2 days of "still pending" are inflated — those subs aren't 36h old yet.

---

*Source of truth: `u36_turnaround_report.Rmd`. If a query drifts, that file wins.*
