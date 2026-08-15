# Backups and restore

The backup service creates a PostgreSQL custom-format logical dump, verifies its catalog, encrypts it with a file-mounted passphrase using OpenPGP AES-256/SHA-512, and writes the ciphertext, SHA-256 file, and JSON manifest to the dedicated TrueNAS dataset. It decrypts to a temporary file, byte-compares the plaintext hash, and runs `pg_restore --list` before status becomes `verified`. The encrypted archive is published last by an atomic same-dataset rename, so an interrupted write cannot appear under a complete archive filename.

On the configured weekly interval, the service selects a verified archive, binds its exact filename/hash to the manifest, checks ciphertext and plaintext hashes, creates a fresh isolated test database, restores with `--exit-on-error`, requires the migration revision and public-table count to equal the source manifest, and drops the test database. File existence never constitutes restore verification.

Machine-readable files in `/mnt/Apps/PowerMeterV2/backups/status` are:

- `last-backup-attempt.json`
- `last-successful-backup.json`
- `last-restore-test-attempt.json`
- `last-successful-restore-test.json`

The API mounts that directory read-only and distinguishes unavailable, invalid,
failed, and verified evidence. The UI/operations check compares verified
timestamps with the configured schedule before treating evidence as current.
Backup and restore-test failures generate debounced alerts.

## Normal no-shell verification

The backup service creates an initial verified encrypted backup and isolated
restore evidence and then follows its configured schedule. In authenticated
**Settings > Backups & restore**, require both the latest successful backup and
latest successful isolated restore test to be recent for that schedule. For the
complete machine-readable evidence, open
`https://power-monitor.home.arpa:8443/api/v1/backups/status` in that same
authenticated browser session and save the response securely. Record the run
IDs, UTC timestamps, archive SHA-256, migration revision, and restored table
count. A file in the archive dataset is not, by itself, a verified backup.

Use the TrueNAS Apps UI to inspect the backup workload's typed logs and health.
Normal creation and verification do not require SSH, System Shell,
`docker exec`, or a container console. Do not open an Apps container shell as
root to run backup scripts: that can create archive/status files the configured
backup identity (`568:568`) cannot manage.

An operator-directed recovery is a separate, reviewed incident procedure. It
must restore into a new isolated database, require an explicit target-name
confirmation, validate that database, and use the release-specific cutover in
`deploy/truenas/ROLLBACK.md`. In-place restore over the production database is
refused. Do not repoint or hand-edit signed production YAML.

Keep the encryption key offline; loss makes archives unrecoverable. Ordinary
backups contain database configuration/audit metadata but no raw secret files.
Original customer bill PDFs and full OCR text are never persisted, so they
cannot enter a backup. TrueNAS encrypted snapshots/replication are
complementary, not a substitute.

Default logical retention is 35 days and is configurable. Never delete the only known-good archive during an incident.

## Local candidate evidence

Before publication on 2026-08-14, backup run
`20260814T034111Z-55e55c1d6d34` produced
`powermeter-20260814T034111Z-55e55c1d6d34.dump.gpg` (23,469 bytes; ciphertext
SHA-256 `24969e1a3e0321ae7253ab4f79b8bb90371d1a595a730b3676e8994a01bf2ca3`).
The image healthcheck passed. Automatic isolated restore run
`restore-20260814T034126Z-bd8d053787f1` verified PostgreSQL 17.10, migration
`20260813_0007`, 57 public tables, and five checks, then removed the temporary
database.

Operator-style restore run `restore-20260814T034411Z-4feedd8b8ab7` restored
the same archive into `pm_restore_manual_test`; a direct query recovered seeded
row `00000000-0000-0000-0000-000000000001 | Backup evidence home`. The test
database was dropped, zero `pm_restore_%` databases remained, and the exact
disposable Compose project, containers, three volumes, and network were removed.
This is local disposable evidence, not a target-TrueNAS backup/restore result.
