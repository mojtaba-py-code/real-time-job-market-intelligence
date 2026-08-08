# Demo walkthrough

A ten-minute tour that exercises every layer, from a source to the dashboard.

---

## 1. One command

```bash
python scripts/seed_demo.py --count 5000 --days 180
```

That creates the schema, loads the skill taxonomy, generates a 5 000-posting
corpus with *known* market dynamics, runs it through the full pipeline, exports
the columnar dataset, and prints what the analytics layer recovered.

The generator deliberately animates a few technologies:

```
Python      ↑        FastAPI    ↑↑        Kubernetes ↑↑
Rust        ↑        COBOL      ↓↓        Mainframe  ↓↓        Jenkins ↓
```

If the trend engine reports those directions, the whole path — ingestion,
normalization, skill extraction, daily aggregation, window comparison and
classification — is working.

---

## 2. Follow the data by hand

```bash
python -m app db upgrade
```

```bash
python -m app taxonomy --sync
```

```bash
python -m app ingest --source synthetic --limit 2000
```

The ingestion summary is the pipeline's report card:

```
Source      Status    Received  Created  Updated  Dupes  Rejected  Quality   Sec
synthetic   partial       2000     1846        0    121        33    0.857  9.31
```

- **Dupes** are the aggregator reposts the generator injects — caught by the
  content fingerprint and the MinHash comparison.
- **Rejected** are the deliberately broken records — quarantined with a reason,
  not dropped.
- **Quality** is the weighted six-dimension score for the batch.

Run it again and nothing duplicates:

```bash
python -m app ingest --source synthetic --limit 2000
```

```
Source      Status    Received  Created  Updated  Dupes  Rejected  Quality   Sec
synthetic   partial       2000        0     1846    121        33    0.857  8.94
```

---

## 3. Ask the market questions

```bash
python -m app report --window-days 180
```

```bash
python -m app trends --days 30
```

```bash
python -m app emerging
```

```bash
python -m app analyze --skill fastapi
```

```bash
python -m app fit --role "Python Backend Developer" --skills python,fastapi,postgresql,docker
```

```bash
python -m app quality
```

---

## 4. The API and the dashboard

```bash
python -m app serve
```

- Dashboard: <http://localhost:8000/dashboard/>
- API docs: <http://localhost:8000/docs>

```bash
curl "http://localhost:8000/analytics/market?window_days=90" | head -40
```

```bash
curl "http://localhost:8000/skills/fastapi/explorer?window_days=90"
```

---

## 5. Alerts

```bash
python -m app keys create --name demo --role admin
```

```bash
curl -X POST http://localhost:8000/alerts/rules \
  -H "X-API-Key: $KEY" -H "Content-Type: application/json" \
  -d '{"name":"FastAPI surge","metric":"skill_demand_change_pct","subject":"fastapi",
       "operator":"gt","threshold":10,"window_days":30,"channels":["in_app"]}'
```

```bash
curl -X POST -H "X-API-Key: $KEY" http://localhost:8000/alerts/evaluate
```

```bash
curl -H "X-API-Key: $KEY" http://localhost:8000/alerts/notifications
```

---

## 6. The real-time path

```bash
python -m app worker
```

The worker runs ingestion, expiry, analytics refresh and alert evaluation on
their own schedules, and consumes the events ingestion publishes. With
`JOBINTEL_EVENTS__BACKEND=redis` those events travel through Redis Streams with
a consumer group; without it they use the in-process bus and the same code path.

---

## 7. Everything at once

```bash
docker compose up
```

PostgreSQL, Redis, migrations, the API with the dashboard, and the worker.
