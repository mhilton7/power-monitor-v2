# TrueNAS datasets and runtime permissions

## UI-created ZFS precondition

Create `Apps/PowerMeterV2` and the nine child ZFS datasets listed in
`INSTALLATION.md` through the TrueNAS UI. Use Generic, case-sensitive datasets
with POSIX ACLs. Do not substitute ordinary directories. All Compose binds are
fixed below `/mnt/Apps/PowerMeterV2` and use `create_host_path: false`.

The initializer verifies that every fixed target is an explicit container
mount. A container mount namespace cannot conclusively prove whether a host
path is a ZFS dataset root rather than a same-filesystem directory, so the UI
creation remains an operator-attested precondition until target TrueNAS
deployment evidence is available.

## Exact post-initialization state

The digest-pinned API image runs `initialize` once as root with no network,
read-only root filesystem, all capabilities dropped, and only `CHOWN`,
`FOWNER`, and `DAC_OVERRIDE` added. It mounts only the nine fixed child datasets.
No long-running service has these privileges.

| Path below `/mnt/Apps/PowerMeterV2` | UID:GID | Mode / additional ACL |
|---|---:|---|
| `postgres` | `70:70` | `0700` |
| `config` | `0:0` | `0755`; `Caddyfile` is `0:1000`/`0440`; `postgres-init-roles.sh` is `0:70`/`0440` |
| `firmware` | `10001:10001` | `0750` |
| `backups` | `568:568` | `0750`; access-only `10001:--x` for status traversal |
| `backups/status` | `568:568` | `0750`; access/default `10001:r-x` |
| `logs` | `0:0` | `0711` |
| `logs/application` | `10001:10001` | `0750` |
| `logs/gateway` | `1000:1000` | `0750` |
| `rate-source-artifacts` | `10001:10001` | `0750` |
| `caddy-data`, `caddy-config` | `1000:1000` | `0750` |
| `secrets` | `0:0` | `0711`; exact named readers below |

Secret files are root-owned with mode `0440`, owning group and other denied,
and only these numeric readers:

- UID 70: bootstrap password.
- UIDs 70 and 10001: migrator, API, and worker database passwords.
- UIDs 70 and 568: backup and isolated-restore database passwords.
- UID 10001: session, field-encryption, and OTA-manifest keys.
- UID 568: backup encryption key.
- UID 1000: TLS leaf/chain, private key, and CA certificate.

The temporary SMB stager access is intentionally removed by this exact ACL
reset. Disable the temporary secrets share before app installation; the ACL
reset is defense in depth, not a substitute for closing the share.

## Fail-closed behavior

The initializer refuses a missing/non-mount path, symbolic link, hard-linked
secret, extra secret/config entry, malformed or duplicate key material,
encrypted TLS key, hostname/chain/key mismatch, or incomplete ACL verification.
It never creates a missing host bind, never generates or replaces a secret,
and never prints a secret value or hash. Every other service has an explicit
`service_completed_successfully` dependency on it.
