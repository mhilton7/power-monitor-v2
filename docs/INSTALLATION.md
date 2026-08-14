# Installation

Production installation uses the generated digest-pinned TrueNAS release asset. Follow `deploy/truenas/INSTALLATION.md`; it contains the exact dataset, ACL, secret, DNS, TLS, install, and health-verification sequence.

## Production availability gate

As of the 2026-08-14 local candidate snapshot, no signed GitHub release, public
GHCR manifest digest, or generated real-digest TrueNAS asset exists. The local
Docker image IDs in `docs/TESTING.md` are host-local build identities and must
not be substituted into production YAML. Do not install until one release
provides the signed manifest, three public application-image digests, checksum
and attestation set, compatible firmware release, and generated YAML. The
checked-in `UNPUBLISHED_*` template must continue to fail at pull time.

For local development only:

```powershell
Copy-Item .env.example .env
.\scripts\create_local_secrets.ps1
docker compose -f compose.yaml -f compose.dev.yaml config --quiet
docker compose -f compose.yaml -f compose.dev.yaml up --build --wait
python -m pytest
npm --prefix frontend ci
npm --prefix frontend run check
```

Development credentials and simulated readings must stay in the development stack. They must never be copied into release artifacts or production. Production images are accepted only from the tagged GitHub workflow, by exact registry digest, with SBOM and provenance.

Required production facts:

- HTTPS origin defaults to `https://power-monitor.home.arpa:8443`.
- The certificate SAN contains the exact hostname and every browser/sensor trusts its CA.
- The only published container port is gateway TCP 8443.
- Application timezone defaults to `America/Los_Angeles`; storage and logs use UTC.
- Sensor readings accepted through `pm-protocol/1.0.0` are the sole usage authority.
- A bill PDF is a rate-source artifact only; it can create no History or usage record.
- The API host kernel exposes Landlock ABI 3 or newer and seccomp filter mode. API `/tmp` is a `nodev,noexec,nosuid` tmpfs; otherwise readiness and bill parsing fail closed.

After the API starts, force the parser-boundary self-test and require exit zero:

```sh
docker compose exec api python -m backend.app.bill_rate_import.sandbox_check
```

Do not deploy the repository template `deploy/truenas/power-monitor-v2.yaml`; explicit `UNPUBLISHED_*` sentinels make it non-deployable until the release workflow substitutes actual registry digests.
