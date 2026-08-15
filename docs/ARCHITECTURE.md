# Architecture

PowerMeter V2 separates measurement authority, transport, presentation, rate sources, and cost calculation so no utility document can become electrical evidence.

```text
PZEM-004T → headless ESP32-S3 → microSD immutable journal
                       │ outbound authenticated HTTPS
                       ▼
                  Caddy → FastAPI → PostgreSQL
                              └──→ worker
                       ▼ same-origin HTTPS/SSE
                    React browser

Official SCE source or uploaded SCE bill PDF
                       │ allowlisted reusable prices/rules only
                       ▼
              reviewed immutable rate version
                       │
Authenticated sensor intervals + rate version → estimated cost
```

## Trust boundaries

- The PZEM sensor, device identity, HMAC request, monotonic sequence, and server commit establish measurement evidence. Raw readings are immutable and unique by `(device_id, sequence)`.
- The sensor makes normal outbound HTTPS requests only. It has no normal runtime web server. Browser code never sees device credentials and never connects to a sensor.
- Caddy is the only LAN listener. It terminates verified HTTPS, sets browser security headers, routes API/SSE/device traffic, and serves the frontend through an internal network.
- API routes authenticate browser sessions or device HMAC independently. Server permissions and home/device scope apply even when a UI control is hidden.
- PostgreSQL is the only database and is not exposed to the LAN. Database transactions define acknowledgment and idempotency boundaries. The empty-cluster bootstrap role becomes `NOLOGIN`; schema migration, API, worker, read-only backup, and isolated restore tests each use a distinct role and file secret. Runtime roles have DML but no schema-creation privilege, and the restore role cannot connect to production.
- The worker owns scheduled rollups, cost runs, official-source checks, alert evaluation, and other leased jobs through PostgreSQL advisory locks.
- Bill-rate extraction is a closed module within the existing API service, not an additional service. Untrusted PDF bytes are read only after a bundled launcher applies Landlock filesystem confinement, seccomp network/system-call denial, rlimits, a cleared environment, closed descriptors, and a private tmpfs work directory. Its frozen parser-only runtime cannot read `/app`, `/data`, `/run/secrets`, or DB sockets. Its capped stdout can contain only a versioned closed rate-draft schema and cannot import ingestion, History, gap repair, calibration, rollup, or forecasting code. The API releases the original bytes and full OCR text after the bounded parse; neither can enter persistent storage, even encrypted. Publication is a separate permissioned action.

## Time, missing data, and money

UTC is authoritative for storage and device timestamps. `America/Los_Angeles` is the default IANA timezone for SCE schedule evaluation. Local TOU, season, rate-version, midnight, billing-cycle, and DST boundaries split intervals; the fall-back hour remains two distinct UTC intervals and spring-forward energy is never fabricated.

Missing measurement values remain null; a measured zero remains zero. Live cards use fresh authenticated heartbeat evidence. History and cost use only committed durable intervals. One-CT devices default to `energy_only`; no implicit whole-home aggregate or solar export is produced.

Money is `Decimal`/PostgreSQL `NUMERIC` and rounds only at defined presentation boundaries. Every cost run records the sensor interval evidence, immutable rate-plan version, algorithm version, completeness, and scope.

## Reliability

The firmware measurement and storage loops are independent of network availability. The server accepts idempotent retries and advances a cursor only after commit. Commands are durable, expiring, idempotent, and delivered through authenticated outbound heartbeats/polls. Destructive commands use prepare/commit.

The server is horizontally conservative: a single PostgreSQL database coordinates scheduled job ownership; no Redis or second database is required. Containers are bounded by health checks, memory/CPU/PID limits, read-only roots, dropped capabilities, and graceful stops. See `docs/TRUE_NAS_DEPLOYMENT.md`.

## Data stores

- PostgreSQL: accounts, permissions, devices, immutable readings, derived intervals/rollups/costs, rates, commands, alerts, audit metadata, and backup/restore evidence references.
- Firmware dataset: administrator-approved OTA artifacts and compatibility metadata.
- Rate-source dataset: immutable official-source artifacts and hashes.
- Backup dataset: encrypted logical dumps, hashes, manifests, and machine-readable restore-test evidence.
- Logs dataset: structured JSON with typed event codes and redaction.

There is no bill-original dataset or original-document retention mode. Secrets
are mounted as files and excluded from ordinary backups, diagnostics, and logs.
