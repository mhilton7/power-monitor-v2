# Official SCE rate synchronization

Official synchronization is a server/worker function. Browser and sensor code never fetch or parse utility pages. The only default source is the public SCE TOU page at the exact allowlisted https://www.sce.com URL; sce.com is also allowed solely for an independently validated HTTPS redirect. Search results, arbitrary URLs, third-party summaries, environment proxies, browser scraping, sensor scraping, and cloud OCR are prohibited.

## Network boundary

Every request and redirect hop follows the same fail-closed sequence:

1. Parse the URL and require HTTPS, port 443, no credentials or fragment, a bounded URL, and an exact configured hostname.
2. Resolve the hostname once for that hop and reject the complete answer set if any answer is private, loopback, link-local, multicast, reserved, or otherwise non-global, or if more than 16 unique addresses are returned.
3. Pass that frozen public IP set to a direct network backend. The TCP connection can use only an address in the set; the transport cannot perform a second hostname lookup.
4. Keep the original hostname as the HTTP origin and TLS SNI/certificate-verification name. The default trust store, chain validation, and hostname verification remain enabled.
5. Record the resolved set and connected IP. Reject a transport result whose peer is outside the validated set.

Redirects are not followed implicitly. Only 301, 302, 303, 307, and 308 are recognized; each Location is resolved relative to the prior URL and must pass the full policy independently. The maximum is three by default. Environment proxy variables are not consulted.

The default budgets are a 5-second connect timeout, 15-second read timeout, and 30-second total deadline covering DNS, every redirect, headers, and body. Response headers are capped at 100 fields/65,536 bytes. The uncompressed body is capped at 5,000,000 bytes, Content-Length is checked before streaming, streaming size is checked again, and non-identity content encoding is rejected. A successful response must be exactly HTTP 200 with an allowed HTML/XHTML/PDF media type. These limits are configurable only within the bounds enforced by backend/app/config.py.

## Conditional and immutable evidence

The current ETag and Last-Modified validator are sent as If-None-Match and If-Modified-Since. An unsolicited 304 is rejected. A valid 304 has no body and no SHA-256 of an empty body: the run references the prior immutable revision and creates no artifact, revision, or candidate.

For HTTP 200, the service hashes the exact response before storage. Artifacts use a content-addressed filename, an exclusive temporary file, file flush/fsync, atomic replacement, and post-write hash verification. Existing content-addressed files are byte/hash verified. Source/hash, artifact-per-revision, source URL, and candidate-per-revision uniqueness prevent duplicates. Database races are isolated in savepoints; duplicate 200 responses reuse the existing immutable revision/candidate without leaving the session in a failed transaction.

Rate-source revisions and artifacts reject updates and deletes in both ORM and PostgreSQL triggers. Candidate source, normalized values, validation evidence, and diff are immutable; only the review state/reviewer fields can change. Every run records:

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

A valid changed source creates a normalized sce-rate-candidate/1.0.0 candidate in review_required. Its sce-rate-diff/1.0.0 evidence contains complete before/after normalized values and a bounded changed-path list against the latest approved candidate (or latest prior candidate when no approved candidate exists). Publication remains a separate rates.manage action and manual approval is mandatory. There is no auto-activation path.

Missing holiday semantics, fields, sections, period coverage, units, charges, or layout produce a typed RATE_SYNC_PARSE_FAILED run. The immutable artifact/revision and validation evidence remain available for review, but no candidate or rate assignment is created. This is intentional: the public-page snapshot documented in docs/SCE_REFERENCE_SNAPSHOT.md does not itself prove tariff holiday handling or an effective date, so the service will not infer either.

## Scheduling, alerts, and operations

Administrators with rates.sync invoke **Check now**. The same sync_official_rate_source service is used by the worker. The PostgreSQL transaction-scoped worker lease permits only one scheduled job owner; enabled sources are due at their configured interval, 168 hours by default. A completed success, unchanged response, parse failure, or network failure updates last_checked_at, preventing a 15-second retry storm.

The worker evaluates alerts after synchronization in the same cycle:

- a valid changed candidate opens rate_source_changed with candidate IDs;
- the latest failed run for a source opens rate_sync_failed with run/source/event IDs;
- later successful/unchanged outcomes resolve the failure condition through the normal alert lifecycle.

For an incident, use the run correlation ID and typed evidence. Never disable TLS/DNS checks, increase limits without review, edit an immutable snapshot, copy page values into a published version, or assign a candidate automatically. Rollback is performed by assigning an earlier immutable published version, never by rewriting history.

## Configuration and automated evidence

The supported environment controls are PM_ALLOWED_SCE_HOSTS, PM_RATE_SOURCE_CONNECT_TIMEOUT_SECONDS, PM_RATE_SOURCE_READ_TIMEOUT_SECONDS, PM_RATE_SOURCE_TOTAL_TIMEOUT_SECONDS, PM_RATE_SOURCE_MAX_BYTES, PM_RATE_SOURCE_MAX_HEADER_BYTES, PM_RATE_SOURCE_MAX_HEADER_COUNT, PM_RATE_SOURCE_MAX_REDIRECTS, and PM_RATE_SOURCE_DUE_LIMIT.

backend/tests/test_rate_source_security.py covers URL/DNS policy, mixed/private DNS, connection IP binding, preserved TLS SNI/hostname verification, rebound-peer rejection, total deadline, conditional 304 semantics, strict redirects, and header limits. backend/tests/test_rate_source_sync.py covers strict parsing, immutable snapshot/candidate/audit evidence, duplicate 200 transaction safety, prior-candidate side-by-side diff, 304 revision reuse, parser failure plus alert generation, and weekly due scheduling.
