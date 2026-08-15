# Roll back PowerMeter V2 on TrueNAS

Rollback is release-specific and must be proved independently of forward
migration. A migration report that starts from the previous release and reaches
the current release proves only that forward upgrade path. It does not run old
binaries against the post-upgrade database, prove backward schema
compatibility, or authorize replacing the current YAML with old images.

## Preserve evidence first

1. Put ingestion into application maintenance mode if the affected version can
   still serve authenticated requests. Record the latest accepted cursor for
   every device; sensors retain unacknowledged intervals on microSD.
2. Export redacted diagnostics and preserve container/migration logs.
3. If the database is healthy, create a new encrypted backup and isolated
   restore result. Never overwrite the last known-good pre-upgrade archive.
4. Record the failed and previous release tags, revisions, manifests, image
   digests, migration revisions, ZFS snapshot, backup run IDs, and UTC times.

## Restored rollback only

A direct application-only rollback is not authorized for v0.1.0-rc.3. The
previous YAML and images may be used only after an explicit, release-specific
recovery test validates an isolated clone or restore of the matching
pre-upgrade database snapshot or verified encrypted backup. The tested
procedure must bind that restored database revision to the previous release
tag, revision, complete YAML, and exact image digests. It must never attach old
binaries to the current post-upgrade database.

After that restore/cutover procedure has produced passing evidence:

1. Stop `power-meter-v2` in **Apps > Installed**.
2. Reverify the retained previous release's attestations and `SHA256SUMS`.
3. Preserve the current production database and datasets unchanged. Clone the
   recorded pre-upgrade ZFS snapshot into an isolated recovery target or
   restore the matching verified encrypted backup there; never roll the live
   dataset backward in place.
4. Validate the restored schema revision, table counts, authentication, device
   cursors, History, cost provenance, and secret/permission bindings before it
   accepts sensor traffic.
5. Run the previous release's `prepare-host.sh` against its complete asset
   directory while the app is stopped, then use the release-specific procedure
   to bind the complete previous digest-pinned YAML to the validated restored
   database target.
6. Require PostgreSQL health, migration exit 0, HTTPS readiness, login, signed
   PZEM heartbeat, SSE, committed History, idempotent retry, cost provenance,
   commands, alerts, diagnostics redaction, and backup/restore evidence.
7. Re-enable ingestion only after confirming server acknowledgements did not
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

For v0.1.0-rc.3, v0.1.0-rc.1 is the prior public V2 release. The signed server
rc.2 tag is not a rollback or migration predecessor: release run `31866197054`
failed cross-repository contract validation before it published a server rc.2
GitHub Release, image set, YAML, or deployment smoke. The rc.3 migration gate
therefore selects rc.1 from non-draft public GitHub Release metadata instead of
choosing the newest Git tag.

Rc.3 adds no Alembic migration and retains `pm-protocol/1.0.0`, but those facts
do not prove that rc.1 binaries support a database touched by rc.3. The
rc.1-to-rc.3 release gate proves only forward upgrade. Rollback compatibility
remains unproven, and no rc.1 image/YAML rollback is authorized without the
separately validated, matching pre-upgrade database restore described above.
The GitHub-hosted rc.3 clean-deployment smoke records
`not_exercised_github_hosted_smoke`; do not call rollback passed without that
separate execution evidence. Reinstalling legacy `power-monitor` code or
importing its database wholesale remains unsupported.

Do not roll firmware back solely to match a server. Its signed manifest must
declare compatible protocol, configuration, storage, bootloader, and partition
versions. Simulation is not physical rollback evidence.
