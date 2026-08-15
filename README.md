# PowerMeter V2

PowerMeter V2 is a greenfield, monitoring-only electrical telemetry system for
an ESP32-S3 sensor head and a self-hosted TrueNAS application. Authenticated
PZEM-004T readings are the sole source of electrical usage, History, energy,
and usage-based cost calculations.

Uploaded Southern California Edison bills are accepted only by the isolated
rate importer. It discards customer, usage, meter, balance, payment, and bill
total fields and emits a closed, review-required reusable rate-plan draft. The
original PDF bytes and full OCR text are released after the bounded parse and
are never persisted, even in encrypted form.

## Repository layout

- `backend/`: FastAPI API, PostgreSQL model, migrations, ingestion and pricing.
- `worker/`: database-leased cost, rollup, alert, and maintenance jobs.
- `frontend/`: responsive React/TypeScript interface.
- `gateway/`: non-root Caddy runtime with the unnecessary privileged-port
  file capability removed for the all-capabilities-dropped deployment.
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
named `.secrets/tls_cert.pem` and `.secrets/tls_key.pem` that are readable by
container UID/GID `1000:1000`. Compose file-backed secrets do not remap host
ownership; on a Linux host, make that numeric identity the owner (or grant it a
read ACL) and keep the key at mode `0440`, never world-readable. Never reuse
these development secrets on TrueNAS; follow the production generation and ACL
guide.

## Installation and safety

For a production install, follow the single end-to-end
[TrueNAS guide](deploy/truenas/INSTALLATION.md); the matching GitHub release
also includes that guide, its dataset/secret companions, the tracked Windows
SMB staging helper, and the auditable image-embedded initializer source.
The no-shell flow is the coordinated rc.4 release contract; never combine its
source-tree docs with rc.3 assets. [docs/INSTALLATION.md](docs/INSTALLATION.md)
separates this release path from local development. This product does not switch
loads. Mains wiring and CT installation must be performed de-energized and by
a qualified person in accordance with the equipment instructions and local
code. The current release candidate is not physically certified; see
[docs/FIRMWARE_RELEASES.md](docs/FIRMWARE_RELEASES.md).

## Release state

The latest prior published candidate is
[`v0.1.0-rc.3`](https://github.com/mhilton7/power-monitor-v2/releases/tag/v0.1.0-rc.3).
The audited source candidate is `v0.1.0-rc.4`; it is installable only if its
signed tagged workflow publishes the complete matching asset set.
Stable release remains fail-closed until the hardware identity, electrical
interface, TLS, OTA rollback, outage recovery, and 72-hour soak gates have
machine-readable evidence. A source checkout is never an install artifact:
only an actually published GitHub release can supply the attested GHCR digests
and generated TrueNAS YAML. Local and CI evidence is recorded in
[docs/TESTING.md](docs/TESTING.md); target-TrueNAS and marked-unit results remain
distinct external evidence.
