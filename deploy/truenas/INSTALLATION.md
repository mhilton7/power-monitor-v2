# TrueNAS installation

Use a current TrueNAS Community Edition release that exposes **Apps > Discover Apps > Install via YAML**. TrueNAS performs only basic YAML validation, so perform all preflight steps here before submitting the generated release asset.

> **Publication gate:** the 2026-08-14 local candidate has no signed GitHub
> release, public GHCR manifest digests, or generated real-digest YAML. Local
> Docker image IDs are not portable registry digests. Do not paste the checked-in
> template or attempt production installation until the release workflow has
> published and verified the complete asset set below.

1. Obtain these assets from the same prerelease/release: `power-monitor-v2-<version>.yaml`, `release-manifest.json`, and their checksums/attestations. Do not install the checked-in `power-monitor-v2.yaml`; its `UNPUBLISHED_*` sentinels are deliberately invalid.
2. Verify the release manifest and attestation on an administrative workstation:

   ```sh
   gh attestation verify release-manifest.json --repo mhilton7/power-monitor-v2
   python scripts/verify_release_artifacts.py --manifest release-manifest.json
   ```

3. Create datasets and ACLs exactly as documented in `DATASET_ACLS.md`.
4. Create secret files exactly as documented in `SECRETS.md`.
5. Copy the release assets `Caddyfile` and `postgres-init-roles.sh` to `/mnt/Apps/PowerMeterV2/config/Caddyfile` and `/mnt/Apps/PowerMeterV2/config/postgres-init-roles.sh`, respectively, with owner `root:root`, mode `0644`. Verify the role script checksum against the release manifest; changing it after first initialization does not retroactively alter an existing cluster.
6. Create a local DNS A/AAAA record for `power-monitor.home.arpa` pointing to TrueNAS. Ensure TCP 8443 is free and permitted only on intended LAN/VPN interfaces.
7. Set TrueNAS Apps to use the intended storage pool. Open **Install via YAML**, name the app `power-meter-v2`, paste the complete generated YAML, and install.
8. Watch containers. `postgres` becomes healthy, the one-shot `migrate` service exits successfully, then `api`, `worker`, `frontend`, `gateway`, and `backup` become healthy. Migration failure blocks application startup by design.
9. From a CA-trusting workstation, run:

   ```sh
   curl --fail --cacert tls-ca.crt https://power-monitor.home.arpa:8443/healthz
   curl --fail --cacert tls-ca.crt https://power-monitor.home.arpa:8443/health/live
   curl --fail --cacert tls-ca.crt https://power-monitor.home.arpa:8443/health/ready
   ```

   The readiness JSON must include `"database":"ready"` and
   `"pdf_sandbox":"enforced"`. The API is intentionally not ready if the host
   cannot enforce Landlock ABI 3+, seccomp, or the hardened `/tmp` tmpfs.
   Force and record a fresh parser-boundary self-test:

   ```sh
   docker compose exec api python -m backend.app.bill_rate_import.sandbox_check
   ```

   It must exit zero with schema `pm-pdf-sandbox-health/1.0.0`.

   The detailed `/api/v1/system/health` endpoint requires an authenticated user
   with `system.view`; verify it after completing first-run bootstrap.

10. Open `https://power-monitor.home.arpa:8443`, create the first owner through the one-time bootstrap flow, configure the home timezone and monitored scope, and enroll a sensor. One-CT devices remain `energy_only` unless an administrator explicitly configures a verified aggregate.
11. Trigger a backup, then an isolated restore test. The system health response must name the exact successful run IDs, timestamps, archive hashes, and restore table count. File existence alone is not verification.

Only gateway TCP 8443 is published. PostgreSQL has no host port and is on an internal network. The browser communicates with the same-origin gateway only; sensors make outbound HTTPS calls only.

Installation is not certified until the deployment suite records clean migration, service health, SSE proxying, upload limits, backup/restore, bind-mount permission, per-service restart, full restart, and rollback evidence on the target TrueNAS host.
