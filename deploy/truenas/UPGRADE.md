# Upgrade PowerMeter V2 on TrueNAS

Upgrade by replacing the complete verified YAML. Never edit only an image tag,
accept a generic image-update suggestion, use a floating tag, or combine assets
from two releases.

## Before the maintenance window

1. Download the new server release into a new empty directory and repeat every
   attestation, `SHA256SUMS`, manifest, protocol, and sentinel check in
   `INSTALLATION.md`.
2. Read the server release notes, migration report, security report, and the
   compatible firmware identity. Confirm the current server/firmware pairing
   still uses `pm-protocol/1.0.0`.
3. Retain the previous release directory, attested manifest, generated YAML,
   `Caddyfile`, PostgreSQL role script, and all exact image digests. Record the
   current application version and database migration revision.
4. Run a fresh encrypted backup and isolated restore test using the commands in
   `INSTALLATION.md`. Stop if either final status is not `verified`; record both
   run IDs and the archive hash.
5. In **Data Protection > Periodic Snapshot Tasks** (or **Storage > Datasets >
   Snapshots** for a one-time snapshot), create a recursive snapshot of
   `Apps/PowerMeterV2` named for the UTC time and old version. Ensure it remains
   inside equally protected encrypted storage. A snapshot supplements, but
   does not replace, the verified logical backup.

## Apply the upgrade

1. Schedule downtime and stop `power-meter-v2` from **Apps > Installed**.
2. Transfer the complete new verified release directory to a temporary TrueNAS
   path. Run its host preparation script while the app is stopped:

   ```sh
   cd /tmp/powermeter-vNEW_VERSION
   sudo bash ./prepare-host.sh --assets "$PWD" --hostname power-monitor.home.arpa
   ```

   This atomically installs the release's Caddy configuration and role script
   and rechecks the existing datasets, secrets, TLS, ownership, and ACLs. The
   role script initializes only an empty PostgreSQL cluster; migrations, not
   edits to that script, update existing clusters.
3. In **Apps > Installed**, select `power-meter-v2`, choose **Edit**, replace the
   entire **Custom Config** with the new generated YAML, and save. If the UI
   keeps the app stopped after editing, start it once.
4. Watch the service order. `postgres` must become healthy and the one-shot
   `migrate` container must exit 0 before the other services start. Preserve
   logs and stop the procedure if migration fails; do not repeatedly redeploy.

## Prove the upgraded instance

Repeat the installation health checks and require:

- verified TLS chain/hostname, `/healthz`, liveness, readiness, database, and
  PDF sandbox;
- owner login and permission enforcement;
- an authenticated PZEM heartbeat, SSE live update, committed History interval,
  and idempotent retry of an already accepted sequence;
- unchanged historical cost provenance across the upgrade and a current exact
  rate fixture;
- command delivery and firmware inventory;
- redacted diagnostics;
- successful restart of each long-running service and one full app stop/start;
- a new encrypted backup and isolated restore test under the new version.

Record the old/new tags, revisions, image digests, migration revisions, snapshot
name, test time, backup/restore run IDs, and operator. Keep the prior release
assets, snapshot, logical archive, and old backup key for the documented
rollback window.

Server upgrade does not authorize an automatic firmware change. Deploy OTA only
when the server release manifest identifies a compatible signed firmware
release and the firmware's protocol/config/storage compatibility permits it.
