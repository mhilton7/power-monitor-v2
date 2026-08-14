#!/usr/bin/env bash
set -Eeuo pipefail
source /opt/powermeter/common.sh

readonly backup_interval="${PM_BACKUP_INTERVAL_SECONDS:-86400}"
readonly restore_interval="${PM_RESTORE_TEST_INTERVAL_SECONDS:-604800}"
[[ "$backup_interval" =~ ^[0-9]+$ ]] && (( backup_interval >= 300 ))
[[ "$restore_interval" =~ ^[0-9]+$ ]] && (( restore_interval >= 3600 ))

last_restore_epoch=0
while true; do
  if /opt/powermeter/backup.sh; then
    now_epoch="$(date +%s)"
    if (( now_epoch - last_restore_epoch >= restore_interval )); then
      latest="$(find "$ARCHIVE_DIR" -maxdepth 1 -type f -name 'powermeter-*.dump.gpg' -printf '%T@ %p\n' | sort -nr | head -n1 | cut -d' ' -f2-)"
      if [[ -n "$latest" ]] && /opt/powermeter/restore.sh --archive "$latest" --test-isolated; then
        last_restore_epoch="$now_epoch"
      fi
    fi
  fi
  sleep "$backup_interval" &
  wait $!
done
