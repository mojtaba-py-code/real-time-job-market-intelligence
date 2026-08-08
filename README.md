# Real-Time Job Market Intelligence Platform

A production-oriented data platform that turns publicly available job postings
into structured, searchable market intelligence.

It answers questions such as *which technologies are in demand right now*,
*which skills are growing or declining*, *what salaries are attached to which
stack*, *which companies are hiring*, and *how well does a given profile match
the market* — from live data, with the statistics, provenance and confidence
scores that make those answers defensible.

This is a data-engineering system, not a scraper. The bulk of the work happens
between collection and the API: validation, cleaning, normalization,
deduplication, NLP enrichment, quality measurement and analytics.

---

## What it does

```
   permitted sources                                    consumers
  ┌──────────────────┐                              ┌────────────────┐
  │ public APIs      │                              │ REST API       │
  │ RSS / Atom feeds │──┐                        ┌──│ dashboard      │
  │ career endpoints │  │                        │  │ CLI            │
  │ datasets         │  │                        │  │ alerts         │
  │ synthetic corpus │  │                        │  └────────────────┘
  └──────────────────┘  │                        │
                        ▼                        │
   ingestion ▸ validation ▸ normalization ▸ deduplication ▸ NLP ▸ analytics
                        │                        ▲
                        ▼                        │
              PostgreSQL (operational)   Parquet + DuckDB (analytical)
                        │                        ▲
                        └──── Redis: cache, rate limits, event stream ────┘
```

| Capability | What the platform actually does |
|---|---|
| **Pluggable ingestion** | One `JobSource` protocol; API, RSS/Atom, career-page, dataset and synthetic adapters. Adding a source is a YAML entry. |
| **Safe collection** | Every outbound URL is validated *and DNS-resolved* before a socket opens (SSRF guard), `robots.txt` is honoured, requests are rate limited per host and responses are size-capped. |
| **Never lose data** | Invalid records are quarantined with their payload and reason, never dropped. |
| **Normalization** | Locations, salaries (with currency conversion from a dated rate table), companies and job titles are all canonicalised, each with a confidence score. |
| **Deduplication** | Four escalating stages: source identity → canonical URL → content fingerprint → MinHash/LSH near-duplicate detection. |
| **NLP** | A configurable skill taxonomy (123 skills, 15 groups), section-aware skill extraction, title decomposition into canonical role + family + seniority + specialization. |
| **Analytics** | Volume, skill demand, multi-window trends, statistically defined emerging technologies, skill co-occurrence with lift, salaries, geography, remote work, seniority, company hiring, market segments. |
| **Personal intelligence** | Market-fit scoring for a candidate profile: skill coverage, high-value missing skills, matching postings. |
| **Data quality** | Six measured dimensions (completeness, validity, uniqueness, consistency, accuracy, freshness) folded into one weighted score, tracked per source, per field and per batch. |
| **Real time** | Event-driven pipeline over a broker abstraction: Redis Streams in deployments, an in-process bus everywhere else. |
| **Interfaces** | FastAPI (auth, scopes, rate limiting, structured errors), a dependency-free dashboard, and a full CLI. |

---

## Quick start

### Local, no infrastructure

The platform runs on SQLite with an in-process cache and event bus, so a fresh
checkout works with nothing installed but Python.

```bash
pip install -e ".[dev]"
```

```bash
python -m app db upgrade
```

```bash
python -m app taxonomy --sync
```

```bash
python -m app ingest --source synthetic --limit 2000
```

```bash
python -m app report --window-days 180
```

```bash
python -m app serve
```

Or do all of that in one step:

```bash
python scripts/seed_demo.py --count 5000
```

Then open <http://localhost:8000/dashboard/> for the dashboard and
<http://localhost:8000/docs> for the API reference.

### Everything, in containers

```bash
docker compose up
```

That starts PostgreSQL, Redis, the migration job, the API (with the dashboard)
on port 8000 and the worker running ingestion, expiry, analytics refresh and
alert evaluation on their own schedules.

---

## The CLI

```bash
python -m app --help
```

| Command | Purpose |
|---|---|
| `ingest [--source X] [--limit N]` | Fetch, process and store postings |
| `process` | Expiry, counter refresh, columnar export and daily snapshot |
| `deduplicate [--apply]` | Re-scan stored postings for duplicates |
| `search "python berlin"` | Search the corpus |
| `analyze --skill python` | Demand, trend, related skills and salary for one technology |
| `trends --days 30` | Rank skills by how fast demand is changing |
| `emerging` | Technologies growing sharply from a small base |
| `fit --role "Python Developer" --skills python,docker` | Market-fit score for a profile |
| `quality` | Data-quality report per source |
| `report` | Full market report |
| `generate --count 100000` | Write a synthetic dataset with known trends |
| `serve` / `worker` | Run the API / the background worker |
| `db upgrade` | Apply migrations |
| `keys create --name ci --role analyst` | Issue an API key |
| `sources list` | Configured sources and their ingestion state |
| `taxonomy [--sync]` | Inspect or persist the skill taxonomy |

Every read command accepts `--json` for machine-readable output.

---

## The API

```bash
curl "http://localhost:8000/analytics/market?window_days=30"
```

```
GET  /health · /health/live · /health/ready · /version
GET  /jobs · /jobs/{id} · /jobs/search · /jobs/suggest · /jobs/facets · /jobs/{id}/similar
GET  /companies · /companies/{name} · /companies/{name}/jobs
GET  /skills · /skills/taxonomy · /skills/{slug}/trend · /skills/{slug}/explorer
GET  /analytics/market · /volume · /skills · /skills/trends · /emerging-skills
     /co-occurrence · /skill-graph · /salaries · /salaries/by/{dimension}
     /locations · /remote · /seniority · /segments · /companies
GET  POST PUT DELETE /alerts/rules · POST /alerts/evaluate · GET /alerts/notifications
GET  POST DELETE /profiles · POST /profiles/market-fit
POST /admin/keys · /admin/ingest/{source} · /admin/taxonomy/sync · GET /admin/stats
```

Authentication is an API key in the `X-API-Key` header. Read endpoints answer
anonymous requests outside production so a fresh installation is explorable;
every write and every admin endpoint always requires a scoped key.

See [API.md](API.md) for the full reference.

---

## How it is built

```
app/
├── core/          configuration, structured logging, errors, crypto, text, time
├── models/        enums, raw records, the canonical job schema, analytics DTOs
├── ingestion/     source protocol, adapters, SSRF-guarded HTTP client, scheduler
├── processing/    cleaning · validation · normalization · deduplication
├── nlp/           skill taxonomy, extraction, title normalization, classifiers
├── analytics/     market · trends · emerging · co-occurrence · salary · geography
├── quality/       the six-dimension data-quality scorer
├── storage/       ORM schema, repositories, cache, Parquet + DuckDB
├── search/        search backends (SQL today, Elasticsearch-shaped interface)
├── events/        broker abstraction (in-memory, Redis Streams)
├── services/      orchestration: ingestion, analytics, jobs, alerts, profiles, admin
├── workers/       scheduler process and the event consumer
├── alerts/        rule evaluation and delivery channels
├── api/           FastAPI app, middleware, dependencies, routers
└── cli/           the `jobintel` command line
```

Each layer depends only on the ones below it. The processing pipeline performs
no I/O at all, which is why the entire data path is unit-testable without a
database. [ARCHITECTURE.md](ARCHITECTURE.md) explains the decisions in detail.

---

## Configuration

Everything is environment-driven; [`.env.example`](.env.example) lists every
setting with placeholder values. Nested settings use a double underscore:

```bash
JOBINTEL_DATABASE__URL=postgresql+asyncpg://user:pass@host/jobintel
JOBINTEL_CACHE__URL=redis://redis:6379/0
JOBINTEL_EVENTS__BACKEND=redis
```

Configuration is validated at startup, and staging/production refuse to boot
with an unset secret key, debug mode on, wildcard CORS or SQLite.

Data sources live in [`configs/sources.yaml`](configs/sources.yaml), the skill
vocabulary in [`configs/skill_taxonomy.yaml`](configs/skill_taxonomy.yaml),
title rules in [`configs/title_rules.yaml`](configs/title_rules.yaml) and the
dated exchange rates in [`configs/currency_rates.yaml`](configs/currency_rates.yaml).
None of them require a code change to extend.

---

## Testing

```bash
pytest
```

```bash
pytest --cov=app --cov-report=term-missing
```

```bash
JOBINTEL_RUN_PERF=1 pytest tests/performance -q -s
```

The suite is split into `unit`, `integration`, `e2e`, `security` and
`performance`. The security tests are the ones worth guarding: they pin down
SSRF rejection, XXE refusal, path-traversal blocking, SQL-injection safety,
credential hashing, scope enforcement and log redaction.

---

## Data ethics and limits

- Only sources that permit automated access are configured. The platform never
  bypasses CAPTCHAs, authentication, anti-bot systems or `robots.txt`.
- No resumes, no candidate profiles, no contact details are collected. The
  canonical schema has no column for them.
- **Salaries are never invented.** A posting without disclosed pay stays
  `provenance = unknown`; statistics report their sample size and the share of
  postings that actually disclosed a figure.
- Currency conversion uses a dated snapshot committed as configuration, so any
  derived figure can be traced back to the rates it came from.
- Market-fit scores are descriptive analytics over public postings, not a
  prediction of employment outcomes — the API says so in every response.

---

## Documentation

| Document | Contents |
|---|---|
| [ARCHITECTURE.md](ARCHITECTURE.md) | Layers, data flow, the job lifecycle and the decisions behind them |
| [API.md](API.md) | Endpoint reference, authentication, pagination, error format |
| [SECURITY.md](SECURITY.md) | Threat model, controls, and how to report a vulnerability |
| [PERFORMANCE.md](PERFORMANCE.md) | Measured throughput, scaling behaviour and tuning |
| [docs/DEMO.md](docs/DEMO.md) | A ten-minute walkthrough of every layer |
| [docs/OPERATIONS.md](docs/OPERATIONS.md) | Running it: health signals, routine tasks, common situations |

---

## License

MIT. See [LICENSE](LICENSE).
