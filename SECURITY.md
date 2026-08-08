# Security

The platform makes outbound requests to URLs it is configured with, parses
untrusted documents, stores credentials and exposes an HTTP API. Each of those
is a place where a mistake is expensive, so each has a control and a test that
proves the control still works.

---

## Reporting a vulnerability

Please **do not** open a public issue. Email the maintainer with:

- what the issue is and where in the code it lives,
- how to reproduce it,
- what an attacker could achieve.

Expect an acknowledgement within 72 hours and a fix or a mitigation plan within
14 days for anything exploitable.

---

## Threat model

| Asset | Threat | Control |
|---|---|---|
| Internal network | SSRF through a configured or redirected URL | `UrlGuard`: scheme allow-list, DNS resolution before connecting, private/link-local/metadata address rejection, manual redirect following with re-validation |
| Process memory | Resource exhaustion from a hostile response | Streaming reads with a byte budget, `Content-Length` pre-check, request timeouts, bounded concurrency |
| XML parser | Entity expansion (billion laughs) and XXE | DTD/entity declarations rejected before parsing; input size bounded by the HTTP budget |
| Filesystem | Path traversal via a dataset path | `resolve_within()` — resolve, then verify the result is inside an allow-listed root |
| Database | SQL injection | Every query is built with SQLAlchemy expressions and bound parameters; no string-built SQL reaches the operational database |
| Credentials | Key theft from the database | API keys stored as Argon2id hashes, never in plaintext; only a public key id is indexed |
| Credentials | Brute force | Argon2id is memory-hard; key lookup is a single indexed query followed by one hash comparison |
| API | Abuse and scraping | Fixed-window rate limiting per API key or client address, shared across replicas when Redis is configured |
| API | Information disclosure | Structured errors with stable codes; internal details are logged, never returned |
| Logs | Secret and PII leakage | Every log field passes through a redactor that removes credential-shaped keys and masks e-mails, phone numbers and bearer tokens |
| Configuration | Insecure deployment | Startup validation refuses production with an unset secret key, debug mode, wildcard CORS, SQLite, or private-network egress enabled |
| Browser | XSS and clickjacking | Strict CSP, `X-Frame-Options: DENY`, `nosniff`, `no-referrer`; the dashboard loads no third-party code |

---

## Controls in detail

### Outbound requests (SSRF)

Every URL the platform fetches — configured sources *and* user-supplied alert
webhooks — goes through the same guard:

1. Scheme must be in the allow-list (HTTPS only by default).
2. Hostname must not be a known-internal name (`localhost`,
   `metadata.google.internal`, …).
3. Embedded credentials (`https://user:pass@host/`) are rejected.
4. The hostname is **resolved**, and every resulting address must be publicly
   routable. This is the step that catches `internal.example.com` pointing at
   `10.0.0.5`.
5. Redirects are followed manually, at most three hops, re-running the whole
   check on each one.

`allow_private_networks` exists for local development and is rejected outright
in staging and production.

### Untrusted documents

RSS/Atom feeds are scanned for `<!DOCTYPE` and `<!ENTITY` before they reach the
parser, and refused if either is present. That removes entity expansion and
external-entity file reads without adding a dependency. Combined with the
response byte budget, a hostile feed cannot exhaust memory either.

### Credentials

```
key presented:  jmi_<key_id>_<secret>
stored:         key_id (indexed) + Argon2id(full key)
```

The key id is public and non-secret; it turns authentication into one indexed
lookup plus one hash verification, instead of comparing secrets across every
row. Keys can carry an expiry, can be revoked, and record their last use.
Scopes are checked per endpoint; the `admin` scope implies the others.

Access tokens, where used, are HMAC-SHA256 signed compact documents with an
expiry and a version. Signature comparison is constant time, and the payload is
validated before anything in it is trusted.

### Secrets

No secret is ever committed. `.env` is git-ignored, `.env.example` contains
placeholders only, and a source that needs a credential names an *environment
variable* in `configs/sources.yaml` rather than carrying the value. CI fails the
build if an AWS key or a private key appears in the tree, or if `.env` is
committed.

The settings object stores secrets as `SecretStr`, so they do not appear in
`repr()`, logs or error messages.

### Logging and privacy

Structured logs pass through a redactor before serialisation:

- keys matching `password|secret|token|api_key|authorization|cookie|…` are
  replaced with `[redacted]`;
- e-mail addresses are masked, phone-shaped digit runs are removed, and
  `Bearer <token>` becomes `Bearer [redacted]`.

The platform does not collect resumes, candidate profiles, contact details or
authentication credentials from postings — the canonical schema has no column
for any of them, and a test asserts that.

### The API

- Security headers on every response, including a CSP that permits no
  third-party origin.
- Request bodies larger than the configured limit are refused before buffering.
- Rate limits are keyed by API key id when present, client address otherwise,
  so one noisy caller cannot exhaust another's budget.
- Every response carries an `X-Request-ID` that also appears in the logs and in
  error bodies — enough to investigate an incident, useless to an attacker.
- Interactive docs are disabled automatically in staging and production.

### Database

- Least privilege: the application user needs `SELECT/INSERT/UPDATE/DELETE` on
  its own schema and nothing else. Migrations can run under a separate role.
- Foreign keys have explicit `ondelete` policies; SQLite has them enforced via
  `PRAGMA foreign_keys=ON` on every connection.
- Check constraints reject impossible values (salary bounds out of order,
  confidence outside `[0, 1]`) at the storage layer.
- `drop_all()` refuses to run in a production-like environment.

---

## Verifying the controls

```bash
pytest tests/security -q
```

Forty-six tests covering SSRF rejection, scheme and credential filtering,
address classification, XXE refusal, path traversal, SQL injection, credential
hashing, malformed-key handling, scope enforcement, security headers, body
limits, error-message hygiene and log redaction.

```bash
bandit -c pyproject.toml -r app -ll
```

```bash
pip-audit
```

Both run in CI on every push, together with the security test suite and a
credential scan of the working tree.

---

## Deployment checklist

- [ ] `JOBINTEL_SECURITY__SECRET_KEY` set to 48+ random bytes, unique per environment
- [ ] `JOBINTEL_ENVIRONMENT=production` (this enables the startup checks)
- [ ] PostgreSQL, not SQLite; credentials from a secret manager, not a file
- [ ] `JOBINTEL_API__CORS_ORIGINS` set to the exact origins that need it
- [ ] TLS terminated in front of the API; HSTS left enabled
- [ ] Rate limits reviewed for the expected traffic
- [ ] The bootstrap admin key rotated and replaced with scoped keys
- [ ] Redis reachable only from the application network
- [ ] Log shipping configured; verify no `[redacted]` markers are missing
- [ ] Container runs as the non-root `jobintel` user (it does by default)
- [ ] `docker compose` overridden with real secrets — the defaults in the file
      are development values and are labelled as such

---

## Supported versions

| Version | Supported |
|---|---|
| 1.0.x | Yes |
