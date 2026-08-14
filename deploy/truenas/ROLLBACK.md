# TrueNAS rollback

Application rollback is possible only when the previous release supports the current database schema. Read the release-specific migration report before changing YAML.

1. Disable device ingestion at the application maintenance control if the affected version can still serve authenticated requests. Record the latest accepted device cursor; sensors retain unacknowledged intervals on microSD.
2. Preserve failed-version diagnostics, then trigger a final backup if the database is healthy. Never overwrite the last known-good backup.
3. If the migration report marks the schema backward-compatible, paste the prior generated YAML with its original digests and wait for all health checks.
4. If it is not backward-compatible, restore into a new PostgreSQL dataset/database from the last pre-upgrade verified archive. Never restore destructively over the only database. Validate schema, row counts, application health, and authenticated ingestion against the isolated copy; then cut over by mounting the validated dataset during a maintenance window.
5. Re-enable ingestion. Confirm that device acknowledgements never regress and that identical retries deduplicate. Verify live heartbeat, History, cost, commands, alerts, and backups.
6. Document the rollback version, digests, reason, start/end UTC, data cursor, backup/restore run IDs, validation results, and operator identity in the incident record.

Do not roll firmware back solely to match a server unless its signed manifest declares compatible protocol/config/storage versions. A hardware rollback result cannot be inferred from simulation.
