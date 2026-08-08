# Operations runbook

Day-to-day operation of a running deployment.

---

## First run

```bash
docker compose up -d
```

The `migrate` service applies the schema; `api` and `worker` wait for it. Then:

```bash
docker compose exec api python -m app taxonomy --sync
```

```bash
docker compose exec api python -m app keys create --name ops --role admin
```

Store that key — it is shown once. Every later key can be issued through
`POST /admin/keys`.

---

## Health and monitoring

| Signal | Where | Alert when |
|---|---|---|
| Liveness | `GET /health/live` | Two consecutive failures |
| Readiness | `GET /health/ready` | Database unreachable |
| Full report | `GET /health` | `status != healthy` |
| Ingestion health | `GET /admin/stats` → `sources[].consecutive_failures` | `>= 3` |
| Freshness | `GET /admin/stats` → `freshness_hours` | Older than twice the ingestion interval |
| Data quality | `GET /admin/stats` → `ingestion.average_quality` | Below 0.6 |
| Rejections | `GET /admin/stats` → `rejections` | A reason spikes suddenly |

Logs are single-line JSON with `timestamp`, `level`, `logger`, `event`,
`service` and a `request_id` that ties an API response to its log lines. Ship
them anywhere; no regex parsing required.

Useful events to watch: `ingestion.failed`, `ingestion.circuit_open`,
`http.rate_limited`, `worker.handler_failed`, `alerts.delivery_failed`,
`cache.redis_init_failed`.

---

## Routine tasks

The worker runs these on a schedule. Run them by hand when you need to:

```bash
python -m app ingest              # every enabled source
python -m app process             # expiry, counters, columnar export, snapshot
python -m app quality             # data-quality report
python -m app deduplicate --apply # re-scan for duplicates and persist verdicts
```

Prune old bookkeeping (keeps the quarantine and run history bounded):

```bash
curl -X POST -H "X-API-Key: $KEY" "http://localhost:8000/admin/maintenance/prune?keep_days=90"
```

---

## Common situations

### A source keeps failing

After five consecutive failures the circuit opens and the source is skipped
until an operator intervenes.

```bash
python -m app sources list
```

Fix the cause (credentials, an endpoint change, the provider blocking you),
then clear the breaker by re-enabling the source:

```bash
python -m app sources disable my_source && python -m app sources enable my_source
```

### Quality dropped

```bash
python -m app quality
```

The report breaks rejections down by reason and shows per-source counters. A
spike in `description_too_short` or `missing_required_field` usually means the
provider changed their payload — check `configs/sources.yaml` mappings against
a fresh sample.

Quarantined records keep their payload, so once the mapping is fixed the data
can be replayed rather than re-fetched.

### Duplicates are getting through

Lower the threshold or widen the window:

```bash
JOBINTEL_DEDUPLICATION__NEAR_DUPLICATE_THRESHOLD=0.80
JOBINTEL_DEDUPLICATION__CANDIDATE_WINDOW_DAYS=45
```

Then re-scan what is already stored:

```bash
python -m app deduplicate --window-days 45 --apply
```

### Analytics look stale

```bash
curl -X POST -H "X-API-Key: $KEY" http://localhost:8000/admin/cache/invalidate
```

If they are still stale, the worker is not running — check `worker.ready` in
its logs.

### The API is slow

1. `GET /admin/stats` → is an ingestion run saturating the database?
2. Is Redis configured? Without it, every replica computes analytics separately.
3. Raise `CACHE__ANALYTICS_TTL_SECONDS`, or serve the dashboard from
   `market_snapshots`.
4. Check `PERFORMANCE.md` for the tuning table.

---

## Backup and restore

```bash
docker compose exec postgres pg_dump -U jobintel jobintel | gzip > backup.sql.gz
```

```bash
gunzip -c backup.sql.gz | docker compose exec -T postgres psql -U jobintel jobintel
```

The Parquet dataset under `/data/analytics` is derived and can always be
rebuilt:

```bash
curl -X POST -H "X-API-Key: $KEY" http://localhost:8000/admin/analytics/export
```

---

## Upgrading

1. Read the migration diff: `python -m alembic history`.
2. Back up the database.
3. Deploy the new image; the `migrate` service runs first.
4. Verify `GET /health` and `GET /version`.
5. Rolling back a schema change: `python -m alembic downgrade -1`.

Migrations use batch mode, so they apply on SQLite as well as PostgreSQL.

---

## Adding a data source

1. Confirm the provider permits automated access.
2. Add an entry to `configs/sources.yaml` — for a credential, name an
   environment variable with `api_key_env` rather than pasting the value.
3. Try it without writing anything:
   ```bash
   python -m app ingest --source my_source --limit 20 --json
   ```
4. Check the mapping quality:
   ```bash
   python -m app quality
   ```
5. Enable it in the config and let the scheduler take over.
