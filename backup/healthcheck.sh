#!/usr/bin/env bash
set -Eeuo pipefail
readonly status_file="${PM_BACKUP_DIR:-/backups}/status/last-backup-attempt.json"
readonly restore_status_file="${PM_BACKUP_DIR:-/backups}/status/last-restore-test-attempt.json"
readonly max_age="$(( ${PM_BACKUP_INTERVAL_SECONDS:-86400} * 2 + 900 ))"
readonly restore_max_age="$(( ${PM_RESTORE_TEST_INTERVAL_SECONDS:-604800} + ${PM_BACKUP_INTERVAL_SECONDS:-86400} * 2 + 900 ))"
readonly success_file="${PM_BACKUP_DIR:-/backups}/status/last-successful-backup.json"
readonly restore_success_file="${PM_BACKUP_DIR:-/backups}/status/last-successful-restore-test.json"
[[ -s "$status_file" && -s "$success_file" ]]
jq -e '.format == "pm-backup/1.0.0" and .state == "verified" and (.run_id | type == "string" and length > 0) and (.sha256 | test("^[0-9a-f]{64}$")) and (.verification_checks | type == "array" and length >= 4)' "$status_file" >/dev/null
jq -e '.format == "pm-backup/1.0.0" and .state == "verified" and (.run_id | type == "string" and length > 0) and (.sha256 | test("^[0-9a-f]{64}$")) and (.verification_checks | type == "array" and length >= 4)' "$success_file" >/dev/null
[[ "$(jq -er '.run_id' "$status_file")" == "$(jq -er '.run_id' "$success_file")" ]]
readonly completed="$(jq -er '.completed_at' "$status_file")"
readonly completed_epoch="$(date -d "$completed" +%s)"
readonly now_epoch="$(date +%s)"
(( completed_epoch <= now_epoch + 300 ))
(( now_epoch - completed_epoch <= max_age ))
[[ -s "$restore_status_file" && -s "$restore_success_file" ]]
jq -e '.format == "pm-backup/1.0.0" and .state == "verified" and (.run_id | type == "string" and length > 0) and (.sha256 | test("^[0-9a-f]{64}$")) and (.verification_checks | type == "array" and length >= 5)' "$restore_status_file" >/dev/null
jq -e '.format == "pm-backup/1.0.0" and .state == "verified" and (.run_id | type == "string" and length > 0) and (.sha256 | test("^[0-9a-f]{64}$")) and (.verification_checks | type == "array" and length >= 5)' "$restore_success_file" >/dev/null
[[ "$(jq -er '.run_id' "$restore_status_file")" == "$(jq -er '.run_id' "$restore_success_file")" ]]
readonly restore_completed="$(jq -er '.completed_at' "$restore_status_file")"
readonly restore_completed_epoch="$(date -d "$restore_completed" +%s)"
(( restore_completed_epoch <= now_epoch + 300 ))
(( now_epoch - restore_completed_epoch <= restore_max_age ))
