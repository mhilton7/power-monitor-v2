# Upgrade PowerMeter V2 on TrueNAS

> This UI-only flow is prepared for the complete signed v0.1.0-rc.14 release
> asset set. Public rc.12, rc.11, rc.10, rc.9, rc.8, rc.6, rc.5, and immutable rc.3 assets retain their attached
> procedures. Never mix release asset sets.

Upgrade by replacing the complete verified YAML in the TrueNAS app editor.
Never edit one image tag/digest, accept a generic image-update suggestion, use
`latest`, or combine assets from two releases.

## Before the maintenance window

1. On Windows, download the new release into a new empty directory and perform
   the exact SHA256SUMS and GitHub-attestation verification in `INSTALLATION.md`.
2. Read its release notes, migration/security/deployment reports, firmware
   compatibility, and hardware-certification status. Require the declared
   `pm-protocol/1.0.0` pairing.
3. Retain the complete previous release, including its generated YAML,
   manifest, image digests, checksum set, and operator guides.
4. In PowerMeter **System health**, require a recent verified encrypted backup
   and isolated restore result. Preserve the backup key offline.
5. In the TrueNAS UI, create and retain a recursive, encrypted snapshot of
   `Apps/PowerMeterV2` named with the old version and UTC time. A snapshot
   supplements rather than replaces the verified logical backup.

The coordinated rc.14 release extends public rc.13 to Alembic head
`20260817_0015`; revision 0015 adds structured SCE thresholds and per-sensor
OTA batches while preserving and linking every legacy deployment. No existing
home, sensor, reading, rate, release, deployment, or command is deleted. Upgrades from rc.5 or
earlier still traverse the additive fail-closed revisions. The 0008 preflight can
stop when existing immutable ingestion evidence conflicts, including a raw
reading whose sequence was also recorded as permanent loss or overlapping
permanent-loss ranges for one device. It never deletes, merges, or rewrites
that evidence automatically. It also stops if a legacy database row references
a retained original bill document; the new runtime forbids all persistent
original-bill storage, including encrypted storage, and does not silently
delete an operator's legacy file. Complete and verify the backup and snapshot
above before applying rc.14. If either preflight stops, leave the prior datasets
intact and preserve the exact failure for reviewed recovery; do not edit
evidence or delete a referenced file merely to make the migration pass.

Revision 0011 also stops before changing data if it finds duplicate natural
rate-plan identities, invalid or overlapping assignments, or inconsistent
candidate-review lifecycle evidence. On PostgreSQL it takes write locks before
those preflights and holds them through guard installation. Do not merge plans,
rewrite review provenance, or trim assignment ranges merely to continue; keep
the prior release intact and use a reviewed recovery decision.

Revision 0014 preserves immutable source revisions and published-rate
provenance while allowing an explicit administrator deletion of only an
unreviewed or rejected candidate working record. A protected review or shared
home reference stops deletion; never bypass the guard to perform cleanup.

Revision 0015 backfills each historical firmware deployment into an explicit
`legacy` batch before making that relationship required. Its downgrade removes
only the additive batch and threshold fields and preserves the original
deployment identity, state, progress, evidence, and timestamps.

Unchanged secrets are not restaged and the secrets dataset is not reshared.
Never rotate a database/application/TLS value merely to upgrade. If a future
release explicitly requires coordinated new inputs, its release-specific guide
must provide a reviewed UI/SMB procedure and compatibility evidence.

An rc.3 installation can also have a legacy `bill-rate-source-artifacts`
dataset. Rc.5, rc.6, rc.8, rc.9, rc.10, rc.11, rc.12, and rc.13 do not mount or write it, and the upgrade never deletes it.
Leave it unmounted and unshared; do not export or decrypt its contents. Its
separate retention or deletion requires an explicit operator decision outside
this upgrade rather than an automated migration.

## Apply through the TrueNAS UI

1. Schedule downtime and stop `powermeter-v2` under **Apps > Installed**.
2. Choose **Edit**, replace the entire **Custom Config** with the complete new
   digest-pinned YAML, and save. Start the app if the UI leaves it stopped.
3. Watch **Workloads/Logs**. The new image's `initialize` service must validate
   all existing inputs, atomically install its two embedded configuration
   assets, repair/verify ACLs, and exit 0. PostgreSQL must then become healthy;
   `migrate` must exit 0; the six runtime services must become healthy.
4. Stop on any initializer or migration failure. Preserve the logs and prior
   datasets; do not repeatedly redeploy, broaden permissions, or bypass a gate.

Because the initializer image/digest changes with the release, Compose creates
the new one-shot service for the upgrade. Routine runtime restarts do not rerun
it. Its operations are content-preserving for secrets and idempotent for exact
configuration/metadata.

## Prove the upgraded instance

Using the browser, Windows PowerShell HTTPS checks, PowerMeter UI, and TrueNAS
app UI, require:

- strict TLS chain and `power-monitor.home.arpa` hostname verification;
- `/healthz`, liveness, readiness, database, and PDF sandbox readiness;
- owner login and permission isolation;
- authenticated PZEM heartbeat, committed History, SSE, and idempotent retry;
- unchanged historical cost provenance and current rate fixture;
- command delivery, firmware inventory, and redacted diagnostics;
- successful restarts of each long-running service without rerunning the
  initializer; and
- a new verified encrypted backup and isolated restore result.

Record old/new tags, revisions, image digests, migration revisions, snapshot,
backup/restore run IDs, UTC time, and operator. A server upgrade does not
authorize firmware OTA unless the signed manifests explicitly coordinate it.
