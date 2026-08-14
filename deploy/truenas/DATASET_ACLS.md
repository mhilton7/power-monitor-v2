# TrueNAS datasets and ACLs

PowerMeter V2 never requires world-writable data. Create an Apps dataset at `/mnt/Apps/PowerMeterV2` and the following child datasets/directories before installing the YAML.

| Path | Owner UID:GID | Mode | Writers |
|---|---:|---:|---|
| `postgres` | `70:70` | `0700` | PostgreSQL only |
| `config` | `0:0` | `0755`; files `0644` | administrator only |
| `firmware` | `10001:10001` | `0750` | API and worker |
| `backups` | `568:568` | `0750` | backup service; API sees `status` read-only |
| `backups/status` | `568:568` | `0750` | backup service |
| `logs/application` | `10001:10001` | `0750` | API and worker |
| `logs/gateway` | `1000:1000` | `0750` | Caddy |
| `rate-source-artifacts` | `10001:10001` | `0750` | API and worker |
| `bill-rate-source-artifacts` | `10001:10001` | `0750` | API and worker |
| `caddy-data` | `1000:1000` | `0750` | Caddy |
| `caddy-config` | `1000:1000` | `0750` | Caddy |
| `secrets` | `0:0` | `0711`; individual files below | administrator only |

The numeric IDs are the container identities declared in the Compose file: application `10001`, backup/TrueNAS Apps convention `568`, Caddy `1000`, and PostgreSQL Alpine `70`. Use numeric IDs so container name-service configuration cannot change ownership.

From a TrueNAS shell as an administrator, after verifying `base` resolves exactly to `/mnt/Apps/PowerMeterV2`:

```sh
base=/mnt/Apps/PowerMeterV2
test "$(realpath "$base")" = /mnt/Apps/PowerMeterV2
install -d -o 70 -g 70 -m 0700 "$base/postgres"
install -d -o 0 -g 0 -m 0755 "$base/config"
install -d -o 10001 -g 10001 -m 0750 "$base/firmware" "$base/logs/application" \
  "$base/rate-source-artifacts" "$base/bill-rate-source-artifacts"
install -d -o 568 -g 568 -m 0750 "$base/backups" "$base/backups/status"
setfacl -m u:10001:rX,d:u:10001:rX "$base/backups/status"
install -d -o 1000 -g 1000 -m 0750 "$base/logs/gateway" "$base/caddy-data" "$base/caddy-config"
install -d -o 0 -g 0 -m 0711 "$base/secrets"
install -o 0 -g 0 -m 0644 /path/to/release/Caddyfile "$base/config/Caddyfile"
install -o 0 -g 0 -m 0644 /path/to/release/postgres-init-roles.sh "$base/config/postgres-init-roles.sh"
```

If the pool uses NFSv4 ACLs, set an explicit restricted ACL instead of combining inherited home/share ACLs with POSIX mode bits. Grant `FULL_CONTROL` to `root`; grant the UID listed above `MODIFY` (or read-only for config/API status mounts) on only its listed dataset; disable inheritance from a broadly writable parent; remove `everyone@` write access. Do not use an SMB share for these application datasets. Validate from containers with the deployment test procedure in `docs/TESTING.md`.

Secret files are a special case: keep them `root:root` with baseline mode
`0400` and apply the named per-container read ACLs in `SECRETS.md`. In
particular, each PostgreSQL role password is readable only by PostgreSQL UID
`70` and the one service UID that consumes that role. The bootstrap password is
readable only by UID `70`; a single shared database password is forbidden.

The named access and default ACL on `backups/status` is also required. It lets
the API UID `10001` traverse and read present and future status JSON files while
the Compose bind remains read-only. It does not grant access to encrypted
archives in the parent backup dataset.

ZFS dataset encryption, snapshots, and replication complement application backups but do not replace the encrypted PostgreSQL logical backup and isolated restore test.
