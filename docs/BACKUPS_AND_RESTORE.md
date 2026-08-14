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

## Manual commands

Run scripts as the configured backup identity (`568:568`). On TrueNAS, use the
host-shell `docker exec --user 568:568` procedure in the release
`INSTALLATION.md`; the Apps container-shell UI can open as root and must not be
allowed to create root-owned archive/status files. Inside an already-correct
backup-identity shell, the commands are:

```sh
/opt/powermeter/backup.sh
/opt/powermeter/restore.sh --archive /backups/archives/powermeter-<run>.dump.gpg --test-isolated
```

An operator-directed restore must target a new database and repeat its name in the confirmation:

```sh
/opt/powermeter/restore.sh \
  --archive /backups/archives/powermeter-<run>.dump.gpg \
  --target-database powermeter_recovery_20260813 \
  --confirm RESTORE-powermeter_recovery_20260813
```

In-place restore over the production database is refused. Validate the isolated
database, but do not repoint or hand-edit the signed production YAML. A
schema-incompatible release must provide a tested recovery/cutover procedure;
see `deploy/truenas/ROLLBACK.md`. Keep the encryption key offline; loss makes
archives unrecoverable. Ordinary backups contain database configuration/audit
metadata but no raw secret files or retained customer bill PDFs. TrueNAS
encrypted snapshots/replication are complementary, not a substitute.

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
