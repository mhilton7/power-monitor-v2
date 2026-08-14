# TrueNAS datasets and ACLs

The published YAML uses the fixed host root `/mnt/Apps/PowerMeterV2`. In
TrueNAS, the first component below `/mnt` is the storage-pool name, so this
bundle requires an existing pool named **Apps**. A dataset named `Apps` inside
some other pool is not the same path. Do not hand-edit a signed release YAML to
change the root.

## Create the datasets in the TrueNAS UI

TrueNAS configuration changes should be made through its UI or API. In
**Storage > Datasets**, select the `Apps` pool and create `PowerMeterV2` with:

- **Dataset Preset:** Generic
- **ACL Type:** POSIX (shown under Advanced Options)
- **Case Sensitivity:** Sensitive
- **Encryption:** inherit encrypted storage, or enable encryption here and
  retain its recovery material offline

Create these child datasets with the same Generic/POSIX choice:

```text
Apps/PowerMeterV2/postgres
Apps/PowerMeterV2/config
Apps/PowerMeterV2/firmware
Apps/PowerMeterV2/backups
Apps/PowerMeterV2/logs
Apps/PowerMeterV2/rate-source-artifacts
Apps/PowerMeterV2/bill-rate-source-artifacts
Apps/PowerMeterV2/caddy-data
Apps/PowerMeterV2/caddy-config
Apps/PowerMeterV2/secrets
```

Do not create SMB, NFS, or WebDAV shares for these datasets. The **Apps**
dataset preset creates an NFSv4 ACL and is intentionally not used here; the
release's exact per-container named ACLs are POSIX ACLs. The hidden `ix-apps`
dataset managed by TrueNAS remains separate from these application-data
datasets.

Verify the result from **System > Shell** without changing ZFS configuration:

```sh
zfs list -r -o name,mountpoint,encryption,keystatus Apps/PowerMeterV2
```

Every listed child must have its own matching mount point and every encrypted
dataset must be unlocked before the app starts.

TrueNAS documents the current [dataset creation](https://www.truenas.com/docs/scale/datasets/managingdatasets/)
and [POSIX ACL](https://www.truenas.com/docs/scale/datasets/permissions/configuringacls/)
screens. Use the documentation version matching the installed TrueNAS release.

## Required ownership

PowerMeter V2 never requires world-writable storage. The numeric IDs are the
container identities declared in the release YAML; they do not need matching
named TrueNAS accounts.

| Path below `/mnt/Apps/PowerMeterV2` | Owner UID:GID | Mode | Access |
|---|---:|---:|---|
| root | `0:0` | `0755` | traversal to child datasets |
| `postgres` | `70:70` | `0700` | PostgreSQL only |
| `config` | `0:0` | `0755`; files `0644` | administrator writes; containers read selected mounts |
| `firmware` | `10001:10001` | `0750` | API and worker |
| `backups` | `568:568` | `0750` | backup service only |
| `backups/status` directory | `568:568` | `0750` plus UID `10001` read/default ACL | backup writes; API reads |
| `logs` | `0:0` | `0711` | traversal only |
| `logs/application` directory | `10001:10001` | `0750` | API and worker |
| `logs/gateway` directory | `1000:1000` | `0750` | Caddy |
| `rate-source-artifacts` | `10001:10001` | `0750` | API and worker |
| `bill-rate-source-artifacts` | `10001:10001` | `0750` | API and worker |
| `caddy-data` | `1000:1000` | `0750` | Caddy |
| `caddy-config` | `1000:1000` | `0750` | Caddy |
| `secrets` | `0:0` | `0711`; files use named ACLs | administrator only |

After creating the secret and certificate files described in `SECRETS.md`, run
the release's checked and attested preparation script from a temporary release
asset directory:

```sh
sudo bash ./prepare-host.sh --assets "$PWD" --hostname power-monitor.home.arpa
```

The script refuses to create or guess ZFS datasets. It requires all 11 exact
ZFS mount points, verifies the complete `SHA256SUMS`, installs `Caddyfile` and
`postgres-init-roles.sh`, validates secret formats and the TLS chain/SAN/key,
then applies and rechecks the table above. It never prints secret values.

Secret files remain `root:root`, with owner read plus only the named container
readers in `SECRETS.md`. On POSIX files, the ACL mask is displayed in the group
mode bits, so `stat` commonly shows `0440` after a named reader is added even
though `group::---` and `other::---`; inspect `getfacl`, not only the numeric
mode.

ZFS snapshots and replication complement the encrypted PostgreSQL logical
backup and isolated restore test. They do not replace it.
