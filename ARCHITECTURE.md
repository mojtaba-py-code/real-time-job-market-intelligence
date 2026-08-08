# Architecture

How the platform is put together, and why.

---

## 1. The shape of the system

```
                          ┌────────────────────────┐
                          │   permitted sources    │
                          │  API · RSS · careers   │
                          │  datasets · synthetic  │
                          └───────────┬────────────┘
                                      │  JobSource.fetch_jobs()
                                      ▼
                          ┌────────────────────────┐
                          │  ingestion             │  SSRF guard, robots.txt,
                          │  (app/ingestion)       │  per-host rate limiting,
                          └───────────┬────────────┘  size caps, retries
                                      │  RawJob
                                      ▼
   ┌──────────────────────────────────────────────────────────────────┐
   │  processing pipeline (app/processing) — pure, no I/O             │
   │                                                                  │
   │  cleaning ▸ validation ▸ normalization ▸ deduplication ▸ scoring  │
   │              │                  │                                │
   │              │                  └── NLP (app/nlp)                │
   │              └── quarantine (rejected_records)                   │
   └──────────────────────────────┬───────────────────────────────────┘
                                  │  NormalizedJob
                                  ▼
        ┌──────────────────────────────────────────────┐
        │  services (app/services) — the unit of work   │
        └───────┬───────────────────────────┬──────────┘
                │                           │
                ▼                           ▼
   ┌────────────────────────┐   ┌───────────────────────────┐
   │ PostgreSQL             │   │ Parquet (year/month/day)  │
   │ operational store      │   │ + DuckDB, analytical      │
   └───────────┬────────────┘   └─────────────┬─────────────┘
               │                              │
               └──────────────┬───────────────┘
                              ▼
                ┌──────────────────────────┐
                │ analytics (app/analytics)│
                └─────────────┬────────────┘
                              ▼
        ┌──────────────┬──────────────┬──────────────┐
        │  FastAPI     │  dashboard   │  CLI/alerts  │
        └──────────────┴──────────────┴──────────────┘

     Redis: cache · rate-limit counters · event stream (Redis Streams)
```

---

## 2. Layering rules

Dependencies point one way only:

```
api / cli / workers
        ↓
    services
        ↓
analytics · search · alerts
        ↓
processing · nlp · quality
        ↓
     storage
        ↓
      core
```

Three consequences that shape the whole codebase:

* **The processing pipeline performs no I/O.** `ProcessingPipeline.process()`
  takes a list of `RawJob` and returns a `ProcessingResult`. That is why the
  entire data path — cleaning, validation, normalization, NLP, deduplication,
  quality scoring — is unit-testable without a database, and why the same code
  runs inside a worker, the CLI and the benchmark suite.

* **Repositories are the only place that speaks SQL.** They never open or
  commit transactions; the service layer owns the unit of work, so a batch that
  writes jobs, skills, salaries, quarantined records and quality events either
  lands completely or not at all.

* **Every external system sits behind a protocol.** `JobSource`, `CacheBackend`,
  `EventBus`, `SearchBackend`, `SkillExtractor` and `Notifier` each have at
  least two implementations, one of which needs no infrastructure. That is what
  makes a fresh checkout runnable and a Kafka or Elasticsearch migration a
  one-class change.

---

## 3. The job lifecycle

```
DISCOVERED ──▸ VALIDATED ──▸ NORMALIZED ──▸ DEDUPLICATED ──▸ ENRICHED ──▸ ACTIVE
     │              │                             │                         │
     │              ▼                             ▼                         ▼
     └──────────▸ REJECTED                    DUPLICATE                 UPDATED
                (quarantined,                (kept, linked                  │
                 with reason)                 to the original)              ▼
                                                                        EXPIRED
                                                                            │
                                                             re-listed ─────┘
```

Four timestamps travel with every posting: `first_seen_at`, `last_seen_at`,
`updated_at`, `expired_at`. Together with the `(source, source_job_id)` natural
key they are what make the pipeline idempotent — running the same batch twice
produces updates, not duplicates.

Two subtleties worth naming:

* **A posting is not a duplicate of itself.** Before deduplication runs, the
  ingestion service looks up identifiers already in storage and re-adopts them.
  Without that step, re-ingesting a known posting would match its own stored
  content fingerprint and be filed as a duplicate.
* **Expired postings can come back.** If a source re-lists something the
  platform had retired, the upsert reactivates it instead of creating a second
  record.

---

## 4. Deduplication

Four stages, cheapest first, each optional and each with configurable
thresholds:

| Stage | Signal | Catches |
|---|---|---|
| 1. Source identity | `(source, source_job_id)` | The same feed re-served |
| 2. Canonical URL | Tracking parameters, host and case normalised away | The same posting reached through different links |
| 3. Content fingerprint | SHA-256 over company + title + location + body | The same opening published by two sources |
| 4. Near duplicate | MinHash signature + LSH banding | Reposts with a changed title or a reworded intro |

Stage 4 is the interesting one. Comparing every arrival against the whole
corpus is O(n²); MinHash reduces each posting to a 128-integer signature whose
agreement estimates Jaccard similarity, and LSH banding turns "find similar
documents" into a handful of dictionary lookups. Signatures are computed in one
vectorised NumPy expression, and the comparison window is bounded by
`candidate_window_days`, so the cost per posting stays flat as the corpus grows.

---

## 5. NLP

The pipeline is a chain of injectable components:

```
description ▸ cleaning ▸ tokenization ▸ normalization ▸ skill extraction
            ▸ classification ▸ taxonomy mapping ▸ confidence scores
```

The default skill extractor is a dictionary matcher compiled from
`configs/skill_taxonomy.yaml` into a single alternation regex — linear in the
document length, deterministic, and explainable (every detection carries the
alias that produced it). Three details make it precise rather than naive:

* **Technology-safe boundaries.** Lookarounds instead of `\b`, because `\b`
  splits `c++`, `c#` and `node.js` in the wrong places.
* **Section awareness.** Requirements and nice-to-have headings are located, so
  a skill can be marked required, optional or unclear.
* **Ambiguity handling.** Short names that are also English words (`Go`, `R`,
  `C`) are flagged in the taxonomy and only count when delimited or qualified
  by technical context. "We go to conferences" does not become a Go job.

Because the extractor sits behind the `SkillExtractor` protocol, replacing it
with spaCy, an embedding model or a fine-tuned classifier changes one
constructor argument and nothing else.

Title normalization decomposes rather than flattens: `Senior Backend Python
Engineer (m/f/d)` becomes canonical title `Backend Engineer`, family `Backend
Engineering`, seniority `senior`, specialization `Python`, segment
`backend_engineering` — four independent facts, each with a confidence.

---

## 6. Storage

**PostgreSQL is the operational store.** Seventeen tables covering jobs,
companies, locations, skills, the job/skill association, sources, events,
salary records, market snapshots, skill trends, quality events, ingestion runs,
quarantined records, API keys, alert rules, notifications and candidate
profiles. A few columns (`company_name`, `country_code`, `city`) are
deliberately denormalized onto `jobs`: analytics filters and groups on them
constantly, and joining three tables for every dashboard widget costs far more
than the duplicated bytes.

**Parquet is the analytical store.** Postings are exported to a dataset
partitioned by `year/month/day` and queried with DuckDB. A 90-day question
reads 90 small files instead of the whole corpus, and a query touching three
columns reads three columns.

**Redis is optional everywhere.** Cache, rate-limit counters and the event
stream all have in-process implementations behind the same interface, so a
single-container deployment and the test suite need no Redis at all.

Timestamps deserve a note: a custom `UtcDateTime` type normalises on the way in
*and* on the way out, because PostgreSQL preserves the offset and SQLite does
not. Without it, the first comparison between a stored and a live timestamp
raises `TypeError` — on SQLite only, in production only.

---

## 7. Real-time processing

```
ingestion ──publish──▸ event bus ──consume──▸ worker ──▸ cache invalidation
                                                     ──▸ counter refresh
                                                     ──▸ alert evaluation
```

Events are small: identifiers and a compact payload, never whole documents.
Workers re-read the authoritative record from the database, which keeps the
queue cheap and avoids stale copies. Delivery is at-least-once, so every
handler is idempotent — cache invalidation, snapshot upsert and analytics
recomputation can all run twice with the same result.

The worker process runs two things concurrently: the scheduler (ingestion,
expiry, analytics refresh, alerts, each on its own cadence with jitter) and the
event consumer. Both stop cleanly on `SIGTERM`.

---

## 8. Analytics

Every engine takes a repository and an `AnalyticsFilter`, and returns immutable
value objects. The mathematics lives in `app/analytics/statistics.py` as pure
functions, so the trend classifier and the emerging-technology detector are
tested against known series rather than against a live corpus.

Three principles run through the analytics layer:

* **Like-for-like comparison.** A 7-day window is compared with the 7 days
  before it, never with a different-length period. Gaps are filled with zeros
  before smoothing, so a skill that disappears for a week is not silently
  interpolated over.
* **Say when you do not know.** Percentage change against a zero baseline
  returns `None`, not infinity. A series too short to support a claim is
  labelled `insufficient_data`. A salary sample below the minimum size is
  reported with its size and no statistics.
* **Statistical thresholds, not editorial ones.** "Emerging" means: small
  baseline share, growth above a configured percentage, a volume floor, *and*
  growth that is a z-score outlier relative to every other skill — which is
  what separates a real shift from the whole market expanding.

Skill co-occurrence reports support, both conditional confidences and lift.
Lift is the one that matters: `Python + English` has enormous support and no
information; `Python + FastAPI` has less support and far more lift.

---

## 9. Data quality

Six dimensions, each with an explicit definition, folded into one score with
declared weights:

| Dimension | Definition | Weight |
|---|---|---|
| Completeness | Weighted share of populated fields | 0.25 |
| Validity | Share of records that passed validation | 0.20 |
| Uniqueness | Share of the batch that was not duplicated | 0.15 |
| Consistency | Share of records whose derived fields do not contradict | 0.15 |
| Accuracy | Mean confidence of the parsers and classifiers | 0.15 |
| Freshness | Recency of the postings, scaled by date coverage | 0.10 |

Scores are computed per batch *and* per source, so a regression in one adapter
is attributable instead of being averaged away. Dimensions below their
threshold emit quality events into the database.

---

## 10. The API

FastAPI, with the middleware stack ordered deliberately (Starlette runs
middleware in reverse registration order, so the request-context layer that
assigns the request id every other layer logs is registered last and runs
first):

```
request ▸ request context ▸ security headers ▸ body size limit
        ▸ rate limit ▸ CORS ▸ router ▸ dependencies ▸ handler
```

Services are constructed once at startup and parked on `app.state`; the
dependency providers hand them to routers. Nothing reaches for a global, which
is what lets a test bind a temporary database with
`create_app(settings, database=db)`.

Errors always leave with the same shape:

```json
{"error": {"code": "not_found", "message": "...", "details": {}, "request_id": "..."}}
```

Domain errors carry their own HTTP status and stable code. Unexpected
exceptions are logged with their traceback and answered with a generic message
plus the request id — which is exactly what an operator needs and nothing an
attacker can use.

---

## 11. Extending it

| To add… | Do this | Code change? |
|---|---|---|
| A data source | Add an entry to `configs/sources.yaml` | None |
| A technology or alias | Add it to `configs/skill_taxonomy.yaml` | None |
| A job-title family | Add a rule to `configs/title_rules.yaml` | None |
| A currency | Add a rate to `configs/currency_rates.yaml` | None |
| A source *type* | Implement `BaseJobSource`, register it in the registry | One class |
| A different broker | Implement `EventBus` (publish, consume, ack) | One class |
| Elasticsearch | Implement `SearchBackend`, return it from `build_search_backend` | One class |
| A transformer-based extractor | Implement `SkillExtractor`, pass it to `NlpPipeline` | One class |
| An alert channel | Implement `Notifier`, register it | One class |

---

## 12. Deliberate trade-offs

**SQLite by default.** A portfolio reviewer should be able to clone, install
and run. Production refuses to start on SQLite, so the convenience cannot leak
into a deployment.

**A rule-based NLP layer rather than a model.** It is deterministic, explainable,
needs no GPU and no 500 MB download, and its precision on technology names is
higher than a general-purpose NER model's. The protocol boundary means the
decision is reversible.

**Sequential ingestion across sources.** They share one outbound rate limiter
and one connection pool, and a job-market platform gains nothing from hammering
five providers at once.

**Duplicates are stored, not discarded.** A posting filed as a duplicate keeps
its row, its `duplicate_of` link and its detection kind. "Why is this not in
the count?" is a question the platform can answer.

**Static exchange rates in configuration.** A dated snapshot that every derived
figure can be traced back to beats a live rate nobody recorded. Currencies
missing from the table are simply left un-normalized rather than converted with
a guess.
