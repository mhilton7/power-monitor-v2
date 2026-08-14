# Roll back PowerMeter V2 on TrueNAS

Rollback is release-specific. Read the failed release's migration report before
changing the app. An old image is safe only when that report explicitly says
the previous application supports the current database schema.

## Preserve evidence first

1. Put ingestion into application maintenance mode if the affected version can
   still serve authenticated requests. Record the latest accepted cursor for
   every device; sensors retain unacknowledged intervals on microSD.
2. Export redacted diagnostics and preserve container/migration logs.
3. If the database is healthy, create a new encrypted backup and isolated
   restore result. Never overwrite the last known-good pre-upgrade archive.
4. Record the failed and previous release tags, revisions, manifests, image
   digests, migration revisions, ZFS snapshot, backup run IDs, and UTC times.

## Backward-compatible application rollback

When the migration report explicitly permits the previous app on the current
schema:

1. Stop `power-meter-v2` in **Apps > Installed**.
2. Reverify the retained previous release's attestations and `SHA256SUMS`.
3. Run the previous release's `prepare-host.sh` against its complete asset
   directory while the app is stopped. This restores its exact Caddy
   configuration and revalidates permissions without changing secret values.
4. Choose **Edit**, replace the complete Custom Config with the previous
   generated digest-pinned YAML, save, and start once if necessary.
5. Require PostgreSQL health, migration exit 0, HTTPS readiness, login, signed
   PZEM heartbeat, SSE, committed History, idempotent retry, cost provenance,
   commands, alerts, diagnostics redaction, and backup/restore evidence.
6. Re-enable ingestion only after confirming server acknowledgements did not
   regress. Document the result and retain failed-version evidence.

## Schema-incompatible recovery

Do not paste old YAML over a schema it cannot read, run an undocumented down
migration, roll the live ZFS dataset backward, rename the only production
database, or restore destructively in place. Those actions can lose intervals
already acknowledged by sensors or destroy the only recoverable state.

The bundled restore command deliberately restores only into a new database and
refuses in-place production restore. It can be used to prove a candidate
archive, but switching the signed production YAML to that database is not an
operator shortcut. A release with a non-backward-compatible migration must ship
its own tested forward repair or explicitly tested recovery/cutover procedure.
Until then:

1. Keep the failed production database and all datasets intact.
2. Restore the last pre-upgrade archive into an isolated recovery environment.
3. Validate schema, table counts, authentication, device cursors, History, cost
   provenance, and secrets/permissions without accepting live sensor traffic.
4. Reconcile post-backup accepted sequences from preserved evidence and sensor
   journals before any cutover. Never invent missing readings or move an
   acknowledgement cursor backward.
5. Perform a reviewed maintenance-window cutover only under that release's
   tested recovery procedure.

For the initial release candidate there is no prior V2 version to roll back to;
reinstalling legacy `power-monitor` code or importing its database wholesale is
not supported.

Do not roll firmware back solely to match a server. Its signed manifest must
declare compatible protocol, configuration, storage, bootloader, and partition
versions. Simulation is not physical rollback evidence.
