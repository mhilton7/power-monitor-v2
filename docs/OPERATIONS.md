# Operations

## Evidence classification

Operational status is deployment-specific. Local unit, browser, container, and
disposable backup/restore results in `docs/TESTING.md` demonstrate the candidate
implementation but do not certify a production instance. A production operator
must require current evidence from that instance for health, signed device
traffic, committed History, costs, backups, restores, and alerts. A target
TrueNAS install is not certified until the release digest manifest and the
clean-install, permissions, restart, upgrade, rollback, SSE, upload-limit, and
backup/restore suite have been recorded on that host.

The checked-in deployment template remains intentionally non-deployable while
its image references contain `UNPUBLISHED_*`. Never substitute local Docker
image IDs, floating tags, or hand-entered digests for the generated, attested
release asset.

## Daily checks

Check `/api/v1/system/health` over verified HTTPS for application/protocol
versions, database reachability, sensor evidence, the latest rate-sync state,
open-alert count, and exact last-successful/last-attempted backup and restore
evidence. Use container health checks for worker/gateway status and TrueNAS for
dataset capacity. Compare evidence timestamps with configured schedules;
unavailable, invalid, failed, or stale evidence is not green.

Review structured logs by typed event and correlation ID. Normal retention is 90 days. Export only through the redacted diagnostics workflow; do not copy container files or database rows into support tickets.

## Incident playbooks

| Alert | Verify | Safe response |
|---|---|---|
| Sensor offline/heartbeat delayed | last signed receipt, Wi-Fi reason, TLS/DNS evidence | restore LAN/DNS/TLS; do not erase or reboot-loop; microSD should backfill |
| Reading backlog | oldest/newest/ack/gaps and heartbeat cadence | fix reachability/capacity; preserve unsynchronized data; request `sync_now` only after health |
| PZEM unavailable | typed CRC/timeout/range evidence | arrange de-energized qualified inspection; never substitute bill or fabricated data |
| SD missing/read-only/full/corrupt | card UUID, valid ranges, unacknowledged count | run diagnostics; reclaim only acknowledged segments; format only explicit prepare/commit after loss review |
| Time untrusted | persisted checkpoint, SNTP/server disagreement | correct time/DNS; preserve untrusted sequences as diagnostics, never invent UTC History |
| TLS validation failure | chain, SAN, time, CA fingerprint | repair DNS/certificate/time; never disable chain or hostname verification |
| OTA failed/rolled back | manifest/version/hash/stage/boot evidence | keep previous valid image; stop rollout; preserve command/deployment ID |
| Rate source changed/sync failed | source hash/validators/parser/diff | manual review; do not guess/auto-overwrite immutable versions |
| Backup/restore failed | exact last attempt code, archive/hash, isolated DB result | protect last good archive/key; repair storage/DB; rerun both gates |

### Official-rate sync triage

Use the `RateSyncRun.correlation_id` to inspect only allowlisted run/audit evidence. Network
failures are typed (for example `DNS_NON_PUBLIC`, `PEER_NOT_PINNED`, `TOTAL_TIMEOUT`,
`REDIRECT_LIMIT`, `BODY_TOO_LARGE`) and parser failures identify a missing structural rule
without retaining arbitrary page text. A 304 must reference a prior revision and have zero
response bytes; a repeated 200 hash must reuse its revision. `RATE_SYNC_PARSE_FAILED` means
the source snapshot was retained but no candidate was created. Review the immutable artifact
and parser version; do not retry rapidly, weaken the allowlist, infer the missing value, or
publish directly. The normal 168-hour schedule resumes from the recorded attempt time.

## Maintenance

- Before upgrade: verified backup plus isolated restore, release/attestation validation, V2 ZFS snapshots, previous digests, protocol/firmware compatibility.
- After upgrade/restart: migrations, all health checks, authentication, signed heartbeat/SSE, committed History, duplicate idempotency, cost fixture, commands, backup/restore.
- Capacity: alert before database/log/backup/rate/firmware datasets fill. Never delete unacknowledged sensor records or the only good backup.
- Certificates: renew before expiry, preserve hostname/SAN and CA distribution, validate devices in a staged cohort.
- Keys: rotate through documented overlap; retain old backup keys until archives expire; revoke compromised device/session credentials immediately.

## Recovery principles

Never overwrite the only production database, force a sequence/ack backward, copy legacy application code/data wholesale, import bill usage, disable security verification, or erase a sensor to solve ordinary network failure. Restore into an isolated target and cut over only after evidence. See `deploy/truenas/ROLLBACK.md` and `docs/BACKUPS_AND_RESTORE.md`.
