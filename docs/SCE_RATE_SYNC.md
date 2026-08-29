# Official SCE rate synchronization

Official synchronization is a server/worker function. Browser and sensor code never fetch or parse utility pages. `PM_SCE_RATE_SOURCE_URL` selects the managed public SCE source and defaults to the residential TOU page. Configuration accepts only ordinary HTTPS on the exact allowlisted `www.sce.com`/`sce.com` hosts and approved SCE rate/tariff path families; changing it updates the managed source in place. Search results, arbitrary URLs, third-party summaries, environment proxies, browser scraping, sensor scraping, and cloud OCR are prohibited.

## Network boundary

Every request and redirect hop follows the same fail-closed sequence:

1. Parse the URL and require HTTPS, port 443, no credentials or fragment, a bounded URL, and an exact configured hostname.
2. Resolve the hostname once for that hop and reject the complete answer set if any answer is private, loopback, link-local, multicast, reserved, or otherwise non-global, or if more than 16 unique addresses are returned.
3. Pass that frozen public IP set to a direct network backend. The TCP connection can use only an address in the set; the transport cannot perform a second hostname lookup.
4. Keep the original hostname as the HTTP origin and TLS SNI/certificate-verification name. The default trust store, chain validation, and hostname verification remain enabled.
5. Record the resolved set and connected IP. Reject a transport result whose peer is outside the validated set.

Redirects are not followed implicitly. Only 301, 302, 303, 307, and 308 are recognized; each Location is resolved relative to the prior URL and must pass the full policy independently. The maximum is three by default. Environment proxy variables are not consulted.

The default budgets are a 5-second connect timeout, 15-second read timeout, and 30-second per-fetch total deadline covering DNS, every redirect, headers, and body. The complete check operation has a separate 20-second default/25-second hard maximum, including retries, backoff, artifact validation, and parsing. Exhausting that envelope returns and records a typed `OPERATION_TIMEOUT`; the route-specific Caddy response-header timeout is statically higher at 40 seconds. Response headers are capped at 100 fields/65,536 bytes. The uncompressed body is capped at 5,000,000 bytes, Content-Length is checked before streaming, streaming size is checked again, and non-identity content encoding is rejected. A successful response must be exactly HTTP 200 with an allowed HTML/XHTML/PDF media type. These limits are configurable only within the bounds enforced by backend/app/config.py.

## Conditional and immutable evidence

The current ETag and Last-Modified validator are sent as If-None-Match and If-Modified-Since. An unsolicited 304 is rejected. A valid 304 has no body and no SHA-256 of an empty body: the run references the prior immutable revision and creates no artifact, revision, or candidate.

For HTTP 200, the service hashes the exact response before storage. Artifacts use a content-addressed filename, an exclusive temporary file, file flush/fsync, atomic replacement, and post-write hash verification. Existing content-addressed files are byte/hash verified. Source/hash, artifact-per-revision, source URL, and candidate-per-revision uniqueness prevent duplicates. Database races are isolated in savepoints; duplicate 200 responses reuse the existing immutable revision/candidate without leaving the session in a failed transaction.

Rate-source revisions and artifacts reject updates and deletes in both ORM and PostgreSQL triggers. Candidate source, normalized values, validation evidence, diff, exact-home manual identity, and canonical digest are immutable while a candidate exists. An administrator may permanently delete only an unpublished candidate with no review or a candidate whose only review state is `rejected`; source revision/hash and redacted audit evidence remain. Reviewed, published, activated, shared-home, and published-rate provenance remains undeletable. Exact-home review rows permit only `reviewed -> published -> activated` or `reviewed -> rejected`; PostgreSQL and ORM guards reject rollback and linkage/timestamp rewrites. Review lifecycle state is stored separately per exact home, so a decision for one home cannot publish, activate, reject, delete, or disclose another home's candidate workflow. Every run records:

- source/home and run/correlation IDs;
- start/completion UTC;
- requested/final URL;
- requested and returned validators;
- each redirect, hostname, resolved public IP set, connected IP, and HTTP status;
- response byte count, media type, and artifact SHA-256;
- parser/schema version and validation result;
- revision/candidate IDs, result event, and typed failure code.

The audit event contains IDs and typed outcomes, not downloaded page text or secrets.

## Parsing, candidates, and review

The SCE public-page parser is a strict structural parser. It accepts only the three supported residential TOU plan sections when all of the following are explicit and internally complete:

- summer and winter definitions;
- weekday, weekend, and holiday treatment;
- exact period names and time boundaries with full 00:00-24:00 coverage;
- USD/kWh units and bounded positive decimal prices;
- the daily base service charge;
- the capped baseline-credit rule for TOU-D-4-9PM and TOU-D-5-8PM;
- explicit absence of a baseline credit for TOU-D-PRIME.

It ignores displayed after-credit examples and stores the reusable capped credit rule once. It does not parse account, identity, bill, usage, reading, energy, payment, balance, or customer fields. The public page is not an effective-date authority, so a valid candidate has a null effective date plus effective_date_confirmation_required=true; no date is invented.

A valid changed source creates a normalized sce-rate-candidate/1.0.0 candidate in review_required. Its sce-rate-diff/1.0.0 evidence contains complete before/after normalized values and a bounded changed-path list against the latest exact-home published/activated candidate (or latest prior candidate when no approved candidate exists); an intervening unapproved candidate never becomes the comparison authority. A rates.manage actor must explicitly review a named plan and attest both its official provenance and timezone-aware effective dates, explicitly publish the resulting immutable version, and explicitly activate that version for an exact-home utility account. The actor may instead reject an unpublished candidate, which records an immutable audit decision and resolves its review alert. No network check, parser result, scheduled worker run, review, or publication automatically activates rates.

When the official source is unavailable, a rates.manage actor may create a deterministic manual candidate. The closed request accepts only an official tariff title/identifier, an optional ordinary HTTPS sce.com URL, exact decimals with no more than eight fractional digits, confirmed effective dates, and a complete gap-free schedule. Its canonical JSON SHA-256 becomes immutable provenance; a database unique key makes concurrent submissions idempotent per exact home. No source document, free-form bill data, usage, identity, account, payment, or customer field is accepted. Manual candidates use the same separate review, publish, activate, or reject steps and never fabricate missing values.

Missing holiday semantics, fields, sections, period coverage, units, charges, or layout produce a typed RATE_SYNC_PARSE_FAILED run. The immutable artifact/revision and validation evidence remain available for review, but no candidate or rate assignment is created. This is intentional: the public-page snapshot documented in docs/SCE_REFERENCE_SNAPSHOT.md does not itself prove tariff holiday handling or an effective date, so the service will not infer either.

## Scheduling, alerts, and operations

Administrators with rates.sync invoke **Check now**. Its default is the exact official SCE Time-of-Use Plans catalog URL. The form also accepts a different official SCE rate page for an administrator-selected check, but the server still requires ordinary HTTPS, an exact allowlisted SCE host, an approved rate/tariff path, no credentials, query, or fragment, and the complete SSRF-safe fetch policy described above. The former built-in tiered-plan default is disabled without deleting its saved revisions or audit evidence; explicitly entering that page remains possible for a one-off check.

The same sync_official_rate_source service is used by the worker. A source-specific PostgreSQL advisory transaction lease serializes manual and scheduled refreshes, while the existing worker lease serializes worker cycles. An overlapping refresh fails closed with RATE_SYNC_BUSY. Enabled sources are due at their configured interval, 168 hours by default. Transient DNS/network/timeout failures and HTTP 408/425/429/5xx responses receive at most three attempts with bounded exponential backoff; every attempt still passes the same URL, DNS, redirect, TLS, header, body, and timeout checks. Security, validation, and parse failures are never retried. A completed success, unchanged response, parse failure, or network failure updates last_checked_at, preventing a 15-second retry storm.

One scheduled network result is projected into a separate run row for every home, including homes without sensors. Candidate/run/status queries require an actor-authorized exact home and never return `home_id=NULL` runs. Official last-run/success/failure chronology is scoped to the configured official source, so a later manual candidate cannot mask an official failure; manual provenance remains visible through candidate and last-known-good state. Status also exposes source identity, schedule, active effective range/provenance, and parsed last-known-good source without crossing home boundaries.

Publishing uses one database-unique natural rate-plan identity and one locked next-version allocator shared by SCE candidates and reviewed bill-rate sources. Activation locks the exact-home utility account, closes the prior assignment at the replacement instant, clips a new assignment at an already scheduled future start, and rejects a conflicting equal start. PostgreSQL guards serialize direct/concurrent writers and reject every overlapping range.

The worker evaluates alerts after synchronization in the same cycle:

- a valid changed candidate opens rate_source_changed with candidate IDs;
- the latest failed run for a source opens rate_sync_failed with run/source/event IDs;
- later successful/unchanged outcomes resolve the failure condition through the normal alert lifecycle.

For an incident, use the run correlation ID and typed evidence. Never disable TLS/DNS checks, increase limits without review, edit an immutable snapshot, copy page values into a published version, or assign a candidate automatically. Rollback is performed by assigning an earlier immutable published version, never by rewriting history.

## Configuration and automated evidence

The supported environment controls are `PM_SCE_RATE_SOURCE_URL`, `PM_ALLOWED_SCE_HOSTS`, `PM_RATE_SOURCE_CONNECT_TIMEOUT_SECONDS`, `PM_RATE_SOURCE_READ_TIMEOUT_SECONDS`, `PM_RATE_SOURCE_TOTAL_TIMEOUT_SECONDS`, `PM_RATE_SOURCE_OPERATION_TIMEOUT_SECONDS`, `PM_RATE_SOURCE_MAX_BYTES`, `PM_RATE_SOURCE_MAX_HEADER_BYTES`, `PM_RATE_SOURCE_MAX_HEADER_COUNT`, `PM_RATE_SOURCE_MAX_REDIRECTS`, `PM_RATE_SOURCE_DUE_LIMIT`, `PM_RATE_SOURCE_RETRY_ATTEMPTS`, and `PM_RATE_SOURCE_RETRY_BACKOFF_SECONDS`.

backend/tests/test_rate_source_security.py covers URL/DNS policy, mixed/private DNS, connection IP binding, preserved TLS SNI/hostname verification, rebound-peer rejection, total deadline, conditional 304 semantics, strict redirects, and header limits. backend/tests/test_rate_source_sync.py covers strict parsing, immutable snapshot/candidate/audit evidence, duplicate 200 transaction safety, 304 revision reuse, parser failure plus alert generation, and weekly due scheduling. backend/tests/test_rate_candidate_workflow.py covers exact-home authorization, configurable managed-source identity, bounded operation/retry behavior, official-only status chronology, rejection, approved-predecessor diffs, deterministic manual idempotency, locked publication/version allocation, non-overlapping assignment replacement, and direct PostgreSQL lifecycle guards. backend/tests/test_alerts.py proves rate alerts for sensorless homes and rejection-driven resolution. Migration `20260815_0011` installs the database identities, state/range checks, migration write locks, and PostgreSQL immutability/concurrency triggers.
