# API reference

Base URL: `http://localhost:8000`
Interactive docs: `/docs` (Swagger) and `/redoc`, disabled in production.

---

## Authentication

Send an API key in the `X-API-Key` header:

```bash
curl -H "X-API-Key: jmi_1a2b3c4d5e6f_..." http://localhost:8000/admin/stats
```

Mint one with the CLI:

```bash
python -m app keys create --name analytics-dashboard --role analyst
```

The plaintext key is shown once and never stored — only its Argon2 hash is.

### Scopes

| Scope | Grants |
|---|---|
| `jobs:read` | Listing, retrieval and search |
| `analytics:read` | Every analytics and skills endpoint |
| `alerts:read` | Reading alert rules and notifications |
| `alerts:write` | Creating, updating, deleting and evaluating rules |
| `profiles:write` | Candidate profiles and market-fit analysis |
| `ingestion:write` | Triggering ingestion runs |
| `admin` | Everything, including credential management |

Roles bundle scopes: `viewer` → read-only, `analyst` → read plus alerts and
profiles, `admin` → everything.

Outside production, read-only endpoints (`jobs:read`, `analytics:read`,
`alerts:read`) also answer anonymous requests so a fresh installation is
explorable. Writes and admin endpoints always require a key.

---

## Conventions

### Pagination

List endpoints take `limit` (1–200) and `offset`, and return:

```json
{
  "items": [ ... ],
  "meta": { "total": 1482, "limit": 25, "offset": 0, "returned": 25 }
}
```

### The analytics filter

Every analytics endpoint accepts the same query parameters:

| Parameter | Type | Default | Notes |
|---|---|---|---|
| `window_days` | int 1–1825 | 30 | Look-back window |
| `country_code` | ISO-3166 alpha-2 | — | e.g. `DE` |
| `city` | string | — | Case-insensitive |
| `company` | string | — | Company slug |
| `segment` | enum | — | `backend_engineering`, `data_engineering`, … |
| `seniority` | enum | — | `junior`, `mid`, `senior`, … |
| `remote_type` | enum | — | `remote`, `hybrid`, `onsite`, `unknown` |
| `skill` | slug | — | Restrict to postings requiring a skill |
| `source` | string | — | Restrict to one source |
| `limit` | int 1–500 | 25 | Rows returned |

### Errors

```json
{
  "error": {
    "code": "not_found",
    "message": "job 0000... was not found",
    "details": {"job_id": "0000..."},
    "request_id": "3f2c...",
    "timestamp": "2024-03-01T10:00:00Z"
  }
}
```

| Status | Code | Meaning |
|---|---|---|
| 400 | `invalid_request` | Semantically wrong input |
| 401 | `authentication_failed` | Missing, malformed or revoked key |
| 403 | `permission_denied` | Authenticated, wrong scope |
| 404 | `not_found` | No such resource |
| 413 | `payload_too_large` | Body over the limit |
| 422 | `validation_error` | Failed request or domain validation |
| 429 | `rate_limit_exceeded` | Over budget; see `Retry-After` |
| 500 | `internal_error` | Logged with the request id, details withheld |

### Rate limiting

Responses carry `X-RateLimit-Limit` and `X-RateLimit-Remaining`. Exceeding the
budget returns `429` with `Retry-After`. The window is keyed by API key when
one is present, by client address otherwise.

### Correlation

Every response carries `X-Request-ID`. Send your own to have it propagated into
the logs.

---

## Endpoints

### Health

| Method | Path | Description |
|---|---|---|
| GET | `/health` | Full dependency report; `503` when degraded |
| GET | `/health/live` | Liveness — answers while the process runs |
| GET | `/health/ready` | Readiness — requires a reachable database |
| GET | `/version` | Version and environment |

### Jobs

| Method | Path | Description |
|---|---|---|
| GET | `/jobs` | Filtered, paginated listing |
| GET | `/jobs/search` | Full-text search with ranking |
| GET | `/jobs/suggest?prefix=` | Title auto-completion |
| GET | `/jobs/facets` | Counts per filter value |
| GET | `/jobs/{id}` | One posting with its description |
| GET | `/jobs/{id}/similar` | Postings sharing the most skills |

`/jobs` filters: `company`, `country_code`, `city`, `remote_type`,
`employment_type`, `experience_level`, `segment`, `skill` (repeatable),
`require_all_skills`, `source`, `salary_min`, `salary_max`, `has_salary`,
`posted_within_days`, `published_after`, `published_before`, `sort`,
`descending`.

```bash
curl "http://localhost:8000/jobs?skill=python&skill=docker&remote_type=remote&limit=10"
```

```bash
curl "http://localhost:8000/jobs/search?q=senior%20python&country_code=DE&sort=relevance"
```

Search responses add a `search` block:

```json
{"search": {"backend": "postgres_fts", "took_ms": 12.4, "query": "senior python"}}
```

### Analytics

| Method | Path | Description |
|---|---|---|
| GET | `/analytics/market` | Headline metrics plus the volume series |
| GET | `/analytics/volume` | Daily volume and moving average |
| GET | `/analytics/skills` | Ranked skill demand |
| GET | `/analytics/skills/trends` | Multi-window trends for the top skills |
| GET | `/analytics/emerging-skills` | Statistically emerging technologies |
| GET | `/analytics/co-occurrence` | Skill pairs with support, confidence and lift |
| GET | `/analytics/skill-graph` | Nodes and weighted edges |
| GET | `/analytics/salaries` | Statistics over disclosed salaries |
| GET | `/analytics/salaries/by/{dimension}` | `segment`, `seniority`, `country`, `city`, `company`, `title`, `remote` |
| GET | `/analytics/locations?by=country\|city` | Geographic demand |
| GET | `/analytics/remote` | Remote / hybrid / on-site split |
| GET | `/analytics/seniority` | Experience-level distribution |
| GET | `/analytics/segments` | Comparable market segments |
| GET | `/analytics/companies` | Companies ranked by open postings |

```bash
curl "http://localhost:8000/analytics/market?window_days=30&country_code=DE"
```

A trend response reports every window it computed, with the direction and a
confidence derived from the sample size:

```json
{
  "slug": "fastapi",
  "windows": [
    {"days": 7,  "current_count": 41, "previous_count": 35, "change_pct": 17.14, "direction": "rising"},
    {"days": 30, "current_count": 168, "previous_count": 128, "change_pct": 31.25, "direction": "rising"}
  ],
  "direction": "rising",
  "volatility": 0.31,
  "sample_size": 312,
  "confidence": 0.87
}
```

`change_pct` is `null` when the baseline is zero — the platform does not print
infinite growth.

### Skills

| Method | Path | Description |
|---|---|---|
| GET | `/skills` | Ranked skill demand |
| GET | `/skills/taxonomy` | The configured hierarchy |
| GET | `/skills/{slug}/trend` | Multi-window trend for one skill |
| GET | `/skills/{slug}/explorer` | Trend, related skills, companies, locations, salary, remote share |
| GET | `/skills/{slug}/related` | Co-occurring skills ranked by association |

### Companies

| Method | Path | Description |
|---|---|---|
| GET | `/companies` | Companies that are hiring |
| GET | `/companies/{name}` | Hiring profile: volume, growth, remote share, top skills |
| GET | `/companies/{name}/jobs` | Their open postings |

### Alerts

| Method | Path | Scope |
|---|---|---|
| GET | `/alerts/rules` | `alerts:read` |
| POST | `/alerts/rules` | `alerts:write` |
| GET | `/alerts/rules/{id}` | `alerts:read` |
| PUT | `/alerts/rules/{id}` | `alerts:write` |
| DELETE | `/alerts/rules/{id}` | `alerts:write` |
| POST | `/alerts/evaluate` | `alerts:write` |
| GET | `/alerts/notifications` | `alerts:read` |
| POST | `/alerts/notifications/{id}/read` | `alerts:write` |

```bash
curl -X POST http://localhost:8000/alerts/rules \
  -H "X-API-Key: $JOBINTEL_KEY" -H "Content-Type: application/json" \
  -d '{
        "name": "Python demand surge",
        "metric": "skill_demand_change_pct",
        "subject": "python",
        "operator": "gt",
        "threshold": 10,
        "window_days": 7,
        "channels": ["in_app", "webhook"],
        "webhook_url": "https://hooks.example.com/jobintel"
      }'
```

Metrics: `skill_demand_change_pct`, `skill_job_count`, `emerging_skill`,
`company_job_count`, `average_salary`, `remote_share_pct`, `total_active_jobs`.
Operators: `gt`, `gte`, `lt`, `lte`, `eq`.

Webhook URLs are validated by the same SSRF guard as ingestion; one pointing at
a private address is rejected and the notification is stored with the reason.

### Profiles and market fit

| Method | Path | Description |
|---|---|---|
| GET | `/profiles` | Profiles owned by the calling key |
| POST | `/profiles` | Create or update (keyed by owner + label) |
| GET | `/profiles/{id}` | One profile |
| DELETE | `/profiles/{id}` | Delete |
| GET | `/profiles/{id}/market-fit` | Score a stored profile |
| POST | `/profiles/market-fit` | Score an ad-hoc profile without storing it |

```bash
curl -X POST "http://localhost:8000/profiles/market-fit?window_days=30" \
  -H "X-API-Key: $JOBINTEL_KEY" -H "Content-Type: application/json" \
  -d '{"target_role": "Python Backend Developer",
       "skills": ["python", "fastapi", "postgresql", "docker"],
       "experience_years": 5}'
```

```json
{
  "target_role": "Python Backend Developer",
  "matching_jobs": 148,
  "market_fit_score": 0.71,
  "skill_coverage": 0.78,
  "missing_skills": [{"slug": "aws", "name": "AWS", "job_count": 402, "share": 0.21}],
  "notes": ["This score describes how a profile compares with current public job postings. It is an analytical indicator, not a prediction of employment."]
}
```

### Admin

All require the `admin` scope.

| Method | Path | Description |
|---|---|---|
| GET | `/admin/stats` | Ingestion throughput, source health, quality events |
| POST | `/admin/keys` | Issue an API key |
| GET | `/admin/keys` | List credentials (metadata only) |
| DELETE | `/admin/keys/{key_id}` | Revoke |
| POST | `/admin/ingest/{source}` | Trigger an ingestion run |
| POST | `/admin/expire` | Expire stale postings |
| POST | `/admin/taxonomy/sync` | Persist the skill taxonomy |
| POST | `/admin/analytics/export` | Refresh the columnar store |
| POST | `/admin/analytics/snapshot` | Freeze today's snapshot |
| POST | `/admin/cache/invalidate` | Drop cached analytics |
| POST | `/admin/maintenance/prune` | Prune old runs and quarantined records |

---

## Reading the numbers

- **Salaries cover disclosed compensation only.** `sample_size` and
  `observed_share` tell you how much of the market that is. Nothing is imputed.
- **Amounts are annualised and converted** to the base currency using the dated
  table in `configs/currency_rates.yaml`. A currency missing from that table is
  left un-normalized rather than converted with a guess.
- **A trend needs history.** With fewer observations than the configured
  minimum, `direction` is `insufficient_data` rather than a guess.
- **`confidence` is a sample-size signal**, not a probability of being right.
- **Duplicates are excluded from every count** but still exist in storage with a
  `duplicate_of` link, so "why is this posting not counted?" is answerable.
