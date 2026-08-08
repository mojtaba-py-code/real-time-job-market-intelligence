# Performance

What the platform actually does, measured on the machine it was developed on,
plus how it behaves as the corpus grows and where to turn the dials.

---

## Reproducing these numbers

```bash
python benchmarks/benchmark.py --scale 1000 10000
```

```bash
JOBINTEL_RUN_PERF=1 pytest tests/performance -q -s
```

The benchmark suite times each stage separately, can record peak memory with
`--memory`, and can write results to JSON for tracking regressions:

```bash
python benchmarks/benchmark.py --stage pipeline --scale 50000 --json benchmarks/results/pipeline.json
```

**Reference machine.** A 2014 dual-core laptop: Intel i5-4210U (2 cores /
4 threads, 1.7 GHz), 6 GB RAM, SSD, Windows 10, Python 3.12, SQLite. This is
deliberately modest hardware — the numbers below are a floor, not a ceiling.

---

## Measured throughput

Measured with `python benchmarks/benchmark.py --scale 1000` on the reference
machine, SQLite, single process:

| Stage | Rate | Per record | Notes |
|---|---|---|---|
| Synthetic generation | 7 800 postings/s | 0.13 ms | Realistic bodies, distributions and injected trends |
| NLP pipeline | 377 documents/s | 2.65 ms | Title decomposition + skill extraction + classifiers |
| MinHash signature | 9 700 documents/s | 0.10 ms | 128 permutations, one vectorised NumPy expression |
| LSH lookup | 8 000 queries/s | 0.12 ms | Roughly flat from 1 000 to 20 000 indexed documents |
| **Full processing pipeline** | **203 records/s** | **4.9 ms** | Cleaning → validation → normalization → NLP → dedup → scoring |
| Ingestion, end to end | 121 records/s | 8.3 ms | Adds identity resolution and the database write |
| Parquet export | 739 rows/s | 1.4 ms | Partitioned write, snappy compression |
| Analytics overview | 223 ms | — | Full dashboard payload over ~900 postings, cold cache |

Where the time goes inside the pipeline, per record:

```
NLP (skill extraction, title, classifiers)   2.65 ms   ████████████████████ 54%
cleaning + normalization + validation        ~1.6 ms   ████████████ 33%
MinHash signature + LSH lookup               ~0.2 ms   ██ 5%
quality scoring + bookkeeping                ~0.4 ms   ███ 8%
```

Add `--memory` to record peak memory as well; `tracemalloc` costs 2–4× in
wall-clock time, so it is off by default and the throughput numbers above are
measured without it.

On a modern 8-core server the same pipeline sustains roughly 4–6× these rates,
and the database write stops being incidental once PostgreSQL replaces SQLite.

---

## Scaling behaviour

| Corpus | Ingestion | Storage | Analytics (`/analytics/market`) |
|---|---|---|---|
| 10 K | ~85 s | ~30 MB SQLite / ~4 MB Parquet | ~250 ms |
| 100 K | ~14 min | ~300 MB / ~35 MB | < 1 s on PostgreSQL with the shipped indexes |
| 1 M | ~2.5 h single worker | ~3 GB / ~350 MB | Serve from `market_snapshots` and the Parquet dataset |
| 10 M | Partition by source and run workers in parallel | PostgreSQL partitioned by month + Parquet | DuckDB over Parquet; PostgreSQL for point lookups only |

The important property is that **nothing in the hot path is O(n²)**:

- Identity resolution is a batched indexed lookup on `(source, source_job_id)`.
- Near-duplicate detection compares against an LSH-bucketed window bounded by
  `candidate_window_days` and `max_candidates`, not against the whole corpus.
- Analytics aggregate in SQL with indexes on every grouping column, or scan a
  partition-pruned Parquet dataset.

---

## What makes it fast

**Batching everywhere.** The job repository resolves identities, inserts and
updates with a bounded number of statements regardless of batch size. Chunks
stay under the SQLite parameter ceiling, so the same code works on both
databases.

**One compiled automaton for skill extraction.** Every alias in the taxonomy —
around 300 surface forms — is compiled into a single alternation regex. One
pass over the document, linear in its length, instead of 300 substring
searches.

**Vectorised MinHash.** The permutation modulus is `2**31 - 1` specifically so
that `a * h + b` fits in a uint64 lane; the whole signature is one NumPy
expression rather than a Python loop over 128 permutations. That change alone
took the pipeline from ~55 to ~200 records/s.

**LSH instead of pairwise comparison.** Banding turns "find similar documents"
into a handful of dictionary lookups, so lookup cost is roughly independent of
corpus size.

**Denormalized analytics columns.** `company_name`, `country_code` and `city`
live on `jobs`, so the dashboard's aggregations do not join three tables.

**Columnar analytical storage.** Parquet partitioned by `year/month/day` means
a 90-day question reads 90 small files, and a query touching three columns
reads three columns.

**Cached analytics with prefix invalidation.** Derived answers are cached under
a versioned, namespaced key; ingestion drops the whole family in one prefix
delete rather than guessing which entries went stale.

**Async I/O throughout.** Database, HTTP and Redis access are all
non-blocking, so the API stays responsive while a worker is mid-batch.

---

## Tuning

| Setting | Default | Raise it when… | Lower it when… |
|---|---|---|---|
| `INGESTION__DEFAULT_BATCH_SIZE` | 500 | Sources are fast and memory is plentiful | Memory is tight |
| `INGESTION__MAX_CONCURRENT_REQUESTS` | 4 | Many independent hosts | A provider asks you to slow down |
| `INGESTION__PER_HOST_DELAY_SECONDS` | 1.0 | — | Never below what the provider permits |
| `DEDUPLICATION__MINHASH_PERMUTATIONS` | 128 | Accuracy matters more than speed | Throughput matters more (64 is usually enough) |
| `DEDUPLICATION__CANDIDATE_WINDOW_DAYS` | 30 | Reposts are common and old | Ingestion is slowing down |
| `DEDUPLICATION__MAX_CANDIDATES` | 500 | The corpus is dense with near-duplicates | Memory is tight |
| `NLP__MAX_DESCRIPTION_CHARS` | 60 000 | — | NLP is the bottleneck (most signal is in the first few KB) |
| `CACHE__ANALYTICS_TTL_SECONDS` | 900 | Ingestion is infrequent | Freshness matters more than latency |
| `DATABASE__POOL_SIZE` | 5 | Many concurrent API replicas | The database has connection limits |

### Turning off what you do not need

```bash
JOBINTEL_DEDUPLICATION__ENABLED=false   # ~30% faster ingestion, duplicates land in the corpus
JOBINTEL_NLP__MIN_SKILL_CONFIDENCE=0.7  # fewer, higher-precision skills
```

---

## Scaling out

**More throughput.** Run several workers, each with a disjoint set of sources.
They share PostgreSQL and Redis; the event stream's consumer group distributes
work, and ingestion is idempotent, so an overlap is harmless.

**More read capacity.** The API is stateless — run as many replicas as you
need behind a load balancer. Point them at a PostgreSQL read replica for
analytics, and use Redis so rate limits and caches are shared.

**Bigger analytics.** The `AnalyticalStore` already answers via DuckDB over
Parquet. For a corpus beyond a single node, the same dataset layout is readable
by Spark, Trino or Athena without changing anything.

**Broker.** Redis Streams is the default; the `EventBus` interface (publish,
consume with a group, acknowledge, claim stale) was chosen so a Kafka
implementation is a single class.

---

## Where the remaining headroom is

1. **NLP is over half of the pipeline.** The extractor is already a single automaton;
   the next step is processing batches in a worker pool, which the pure
   pipeline design makes safe — it holds no shared state per record.
2. **SQLite serialises writes.** PostgreSQL removes that ceiling entirely; the
   ORM schema is identical.
3. **Analytics recompute on cache miss.** For a very large corpus, serve the
   dashboard from `market_snapshots` and refresh it on a schedule instead.
4. **The columnar export is a full re-read.** Incremental export keyed on
   `first_seen_at` would make it proportional to new data rather than to the
   corpus.
