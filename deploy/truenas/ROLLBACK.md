# Roll back PowerMeter V2 on TrueNAS

> This UI-oriented source document is prepared for v0.1.0-rc.9. Public rc.8, rc.6, rc.5,
> and immutable older releases retain their own attached instructions. Never
> combine asset sets.

Rollback must be proved independently of forward migration. A report showing
that an old database upgrades to a new release does not prove old binaries can
read a database touched by the new release.

## Preserve evidence first

1. Put ingestion into maintenance mode if authenticated access remains usable;
   record each device's latest accepted cursor. Sensors retain unacknowledged
   intervals on microSD.
2. Export redacted diagnostics and preserve app/migration logs in the UI.
3. Require a new verified encrypted backup and isolated restore result without
   overwriting the last known-good pre-upgrade archive.
4. Record failed/previous tags, revisions, manifests, digests, schema revisions,
   snapshot, backup run IDs, and UTC times.

## Restored rollback only

A direct application-only rollback from v0.1.0-rc.9 to public v0.1.0-rc.8 is
unauthorized without a separate recovery test. Rc.9 advances Alembic head to
`20260816_0013`, and a forward-migration gate cannot prove that older
binaries correctly handle state touched by rc.9; `not_exercised_github_hosted_smoke`
is not rollback evidence. The immutable server rc.2 and rc.4 tags have no
GitHub Releases and are not predecessors.

Historical rc.3 evidence proved only forward rc.1-to-rc.3 upgrade; it does not
authorize rc.3-to-rc.1, rc.5-to-rc.3, rc.6-to-rc.5, rc.8-to-rc.6, or rc.9-to-rc.8 rollback.

Public rc.3 has a seven-service YAML and no `initialize` service. Its attached
instructions use the rc.3 host-preparation contract. The rc.5/rc.6/rc.8/rc.9 Windows/SMB
initializer procedure cannot be combined with rc.3 assets and does not make an
rc.5-to-rc.3 rollback shell-free. Server rc.4 never became an installation
authority: its release assembly was skipped after deployment smoke failed.

Use a previous YAML only after a release-specific recovery test validates an
isolated clone of the matching pre-upgrade ZFS snapshot or verified encrypted
backup. Never attach old binaries to the current post-upgrade database and
never roll the only live dataset backward in place.

After that separate test passes:

1. Stop the app through **Apps > Installed** and reverify the complete previous
   release on Windows.
2. Preserve current production datasets unchanged. Prepare the validated
   restored database as an isolated recovery target through reviewed TrueNAS
   storage/UI operations.
3. Validate schema, row counts, authentication, device cursors, History, cost
   provenance, and secret/permission bindings without accepting sensor traffic.
4. In the TrueNAS app editor, paste the complete previous digest-pinned YAML
   only as part of the tested cutover and follow that release's attached
   preparation instructions. For rc.3, there is no initializer: PostgreSQL must
   become healthy, its one-shot migration must exit 0 against the restored rc.3
   database, and all seven-service runtime checks must pass.
5. Re-enable ingestion only after proving acknowledgements did not regress.

Do not use an undocumented down migration, overwrite the only production
database, restore destructively in place, or infer missing readings. Reconcile
post-backup accepted sequences from preserved server evidence and sensor
journals before any cutover. Firmware rollback is separately governed by its
signed configuration/storage/bootloader/partition compatibility and physical
evidence; never roll it back solely to match a server.
