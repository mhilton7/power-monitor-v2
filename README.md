# PowerMeter V2

PowerMeter V2 is a greenfield, monitoring-only electrical telemetry system for
an ESP32-S3 sensor head and a self-hosted TrueNAS application. Authenticated
PZEM-004T readings are the sole source of electrical usage, History, energy,
and usage-based cost calculations.

Uploaded Southern California Edison bills are accepted only by the isolated
rate importer. It discards customer, usage, meter, balance, payment, and bill
total fields and emits a closed, review-required reusable rate-plan draft.

## Repository layout

- `backend/`: FastAPI API, PostgreSQL model, migrations, ingestion and pricing.
- `worker/`: database-leased cost, rollup, alert, and maintenance jobs.
- `frontend/`: responsive React/TypeScript interface.
- `backup/`: encrypted PostgreSQL backup and isolated restore verification.
- `shared/`: versioned protocol schemas and cross-language authentication vectors.
- `deploy/truenas/`: hardened TrueNAS Compose templates and operator guides.
- `power-monitor-sensor-headless/`: independent nested firmware Git repository.

## Local verification

```powershell
python -m venv .venv
.\.venv\Scripts\python -m pip install -e ".[dev]"
.\.venv\Scripts\python -m ruff check backend worker tests scripts
.\.venv\Scripts\python -m mypy backend worker
.\.venv\Scripts\python -m pytest
npm --prefix frontend ci
npm --prefix frontend run check
docker compose -f compose.yaml -f compose.dev.yaml config --quiet
```

The local Compose stack uses the same distinct bootstrap, migrator, API,
worker, read-only backup, and isolated-restore identities as production. Create
the ignored local file secrets once (the command refuses to overwrite existing
values), then start the stack:

```powershell
.\scripts\create_local_secrets.ps1
docker compose -f compose.yaml -f compose.dev.yaml up --build
```

The optional `gateway` profile additionally requires locally trusted TLS files
named `.secrets/tls_cert.pem` and `.secrets/tls_key.pem`. Never reuse these
development secrets on TrueNAS; follow the production generation and ACL guide.

## Installation and safety

Begin with [docs/INSTALLATION.md](docs/INSTALLATION.md) and the
[TrueNAS guide](deploy/truenas/INSTALLATION.md). This product does not switch
loads. Mains wiring and CT installation must be performed de-energized and by
a qualified person in accordance with the equipment instructions and local
code. The current release candidate is not physically certified; see
[docs/FIRMWARE_RELEASES.md](docs/FIRMWARE_RELEASES.md).

## Release state

The initial version is `0.1.0-rc.1`. Stable release is fail-closed until the
hardware identity, electrical interface, TLS, OTA rollback, outage recovery,
and 72-hour soak gates have machine-readable evidence. The local server,
frontend, PostgreSQL-role, backup/restore, PDF-sandbox, contract, and firmware
simulation/build gates are recorded in [docs/TESTING.md](docs/TESTING.md), but
no signed tag, public GHCR digests, generated real-digest TrueNAS YAML, target
TrueNAS run, or marked-unit certification exists in that snapshot.
