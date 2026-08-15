# Install PowerMeter V2 on TrueNAS

This is the complete operator path for a current TrueNAS Community Edition
release that supports Docker Compose custom apps. The UI labels below match the
current **Apps > Discover Apps > more_vert > Install via YAML** workflow. TrueNAS
performs only basic YAML validation, so complete every preflight before saving
the app. See the current [TrueNAS custom-app documentation](https://www.truenas.com/docs/scale/apps/installcustomappscreens/)
if a later UI moves a control.

The repository file `power-monitor-v2.yaml` is a non-deployable source
template. Its `UNPUBLISHED_*` values are intentional. Install only a generated,
digest-pinned YAML downloaded from one GitHub release.

## 1. Prerequisites

- A healthy TrueNAS system with its Apps service configured and internet access
  to `ghcr.io` for the initial image pull.
- A storage pool named `Apps`. The signed YAML uses the fixed host root
  `/mnt/Apps/PowerMeterV2`.
- An administrative workstation with GitHub CLI (`gh`), `sha256sum`, `jq`, and
  an authenticated channel for transferring public release assets to TrueNAS.
- Local DNS control for `power-monitor.home.arpa` (or the hostname supported by
  the release) and the ability to distribute a private CA to browsers and
  sensors.
- TCP 8443 free on TrueNAS and restricted to the intended LAN/VPN. Do not expose
  this service directly to the public internet.

The server can be installed before a sensor is online, but it creates no usage,
History, energy, or cost without authenticated PZEM-004T readings.

## 2. Download and authenticate one release

On the administrative workstation, set the exact published tag and download
all assets into a new empty directory. Do not mix files from different tags:

```sh
tag=v0.1.0-rc.3
release_dir="powermeter-${tag}"
test ! -e "$release_dir"
mkdir "$release_dir"
gh release download "$tag" --repo mhilton7/power-monitor-v2 --dir "$release_dir"
cd "$release_dir"
```

Verify every checksummed asset against GitHub's build provenance, then verify
the checksum set locally:

```sh
while read -r _ asset; do
  asset=${asset#\*}
  test -f "$asset"
  gh attestation verify "$asset" --repo mhilton7/power-monitor-v2
done < SHA256SUMS
sha256sum --check --strict SHA256SUMS
```

Inspect the release identity and derive the exact YAML filename from the
attested manifest:

```sh
jq -e --arg version "${tag#v}" \
  '.schema == "pm-server-release/1.0.0" and
   .protocol == "pm-protocol/1.0.0" and .version == $version and
   (.images | keys | sort) == ["api","backup","frontend","gateway"] and
   .images.api.name == "ghcr.io/mhilton7/power-monitor-v2-api" and
   .images.frontend.name == "ghcr.io/mhilton7/power-monitor-v2-frontend" and
   .images.gateway.name == "ghcr.io/mhilton7/power-monitor-v2-gateway" and
   .images.backup.name == "ghcr.io/mhilton7/power-monitor-v2-backup" and
   ([.images[].digest] | all(test("^sha256:[0-9a-f]{64}$"))) and
   (.release_status == "candidate_physical_certification_pending" or
    .release_status == "stable_physical_certification_passed")' \
  release-manifest.json
compose_file=$(jq -er '.compose.file' release-manifest.json)
test "$compose_file" = "$(basename "$compose_file")"
test -s "$compose_file"
! grep -q 'UNPUBLISHED\|:latest' "$compose_file"
```

For a release candidate, read `RELEASE_NOTES.md` and
`hardware-certification-status.json` before continuing. Candidate status means
the software artifacts passed their published gates; it does not claim physical
certification of an ESP32/PZEM unit.

## 3. Create storage, secrets, and TLS

Follow [DATASET_ACLS.md](DATASET_ACLS.md) to create the exact 11 ZFS datasets
through **Storage > Datasets**. Use Generic/POSIX datasets, keep them encrypted
and unshared, and do not create the paths as ordinary directories.

Follow [SECRETS.md](SECRETS.md) to create the ten application/database secrets
and three TLS files. Keep the backup key and private CA recovery material
offline. Create a DNS A/AAAA record for `power-monitor.home.arpa` that points
only to reachable TrueNAS LAN/VPN addresses, and install `tls-ca.crt` in each
browser's trust store after comparing its fingerprint.

Transfer the verified release directory to a temporary location on TrueNAS.
For example, while TrueNAS SSH is deliberately enabled for administration:

```sh
cd ..
scp -r "$release_dir" truenas_admin@truenas-host:/tmp/
```

Do not open an SMB/NFS share on the PowerMeter datasets just to transfer these
public assets. In **System > Shell**, prepare and verify the host:

```sh
tag=v0.1.0-rc.3
cd "/tmp/powermeter-${tag}"
sudo bash ./prepare-host.sh --assets "$PWD" --hostname power-monitor.home.arpa
sudo ss -H -ltn 'sport = :8443'
```

`prepare-host.sh` must end with `TrueNAS host preparation passed`. The `ss`
command must print nothing before first installation. The script verifies all
release checksums, dataset mount points, exact UID/GID and ACLs, secret formats,
certificate SAN/chain/expiry/key match, and installs the verified `Caddyfile`
and `postgres-init-roles.sh` as `root:root` mode `0644`.

## 4. Install the generated YAML

1. In TrueNAS, confirm **Apps Service Running** and that the desired Apps pool
   is selected. The hidden Apps service dataset and `/mnt/Apps/PowerMeterV2`
   application datasets are separate.
2. Open **Apps > Discover Apps**.
3. Open the three-dot **more_vert** menu beside **Custom App** and select
   **Install via YAML**. Do not use the single-container guided wizard.
4. Enter the application name `power-meter-v2`.
5. Paste the complete contents of the verified file named by `$compose_file`
   into **Custom Config**. Do not paste `power-monitor-v2.yaml` from Git, alter
   an image, replace a digest, or inline a secret.
6. Click **Save** once and allow the initial multi-architecture image pulls and
   PostgreSQL initialization to finish.

Expected order is: `postgres` becomes healthy; one-shot `migrate` exits with
status 0; then `api`, `worker`, `frontend`, `gateway`, and `backup` become
healthy. The backup container immediately creates a verified encrypted backup
and isolated restore test, so first health can take several minutes. Migration
failure blocks application startup by design.

Use **Apps > Installed > power-meter-v2** to inspect each container's status and
logs. A read-only host-shell view is also available:

```sh
midclt call app.get_instance power-meter-v2 | jq '{name,state,active_workloads}'
sudo docker ps \
  --filter label=com.docker.compose.project=ix-power-meter-v2 \
  --format 'table {{.Label "com.docker.compose.service"}}\t{{.Status}}\t{{.Image}}'
```

If the Docker filter returns nothing, use the TrueNAS Installed Apps UI and its
container logs; do not guess an internal project name or start a second Compose
project manually.

## 5. Verify HTTPS and the security boundary

From a workstation that trusts only the intended CA for this test:

```sh
origin=https://power-monitor.home.arpa:8443
curl --fail --cacert tls-ca.crt "$origin/healthz"
curl --fail --cacert tls-ca.crt "$origin/health/live" | jq -e '.status == "live"'
curl --fail --cacert tls-ca.crt "$origin/health/ready" |
  jq -e '.status == "ready" and .database == "ready" and .pdf_sandbox == "enforced"'
curl --fail --cacert tls-ca.crt "$origin/api/v1/auth/bootstrap/status" |
  jq -e '.required == true'
```

Do not use `curl -k`, accept a browser warning, or substitute an IP address.
Force a fresh parser-sandbox proof from the TrueNAS host as the API UID:

```sh
api_id=$(sudo docker ps \
  --filter label=com.docker.compose.project=ix-power-meter-v2 \
  --filter label=com.docker.compose.service=api --format '{{.ID}}')
test "$(printf '%s\n' "$api_id" | grep -c .)" -eq 1
sudo docker exec --user 10001:10001 "$api_id" \
  python -m backend.app.bill_rate_import.sandbox_check |
  jq -e '.schema_id == "pm-pdf-sandbox-health/1.0.0" and .pdf_sandbox == "enforced"'
```

Readiness intentionally fails if the host kernel cannot enforce Landlock ABI
3+, seccomp, or the hardened `/tmp` boundary. Do not bypass that gate.

## 6. Complete first-run bootstrap

1. Open `https://power-monitor.home.arpa:8443` and create the one-time owner
   with a unique password. Confirm bootstrap status then becomes `false`.
2. Configure the home's IANA timezone, billing-cycle day, and monitored scope.
   A one-CT device remains `energy_only`; never label it whole-home or solar
   aggregate without verified hardware coverage.
3. Use the compatible firmware release recorded in the server release assets.
   Create a short-lived enrollment token and provision the headless sensor by
   USB with the same hostname and CA. Normal firmware operation is outbound
   HTTPS only; there is no sensor web server.
4. Confirm an authenticated PZEM heartbeat, then wait for a committed interval.
   Missing data must remain unavailable and a measured zero must remain zero.
5. Configure and review a rate plan. A bill PDF may contribute reusable prices
   and cost rules only; bill usage, readings, totals, balances, identity, and
   payments never create History or energy.
6. Review the authenticated **System health** view. It must report database
   reachability and exact verified backup/restore run evidence.

## 7. Force a backup and isolated restore test

Run these commands from the TrueNAS host. The explicit UID prevents a root
container shell from creating files the scheduled backup service cannot manage:

```sh
backup_id=$(sudo docker ps \
  --filter label=com.docker.compose.project=ix-power-meter-v2 \
  --filter label=com.docker.compose.service=backup --format '{{.ID}}')
test "$(printf '%s\n' "$backup_id" | grep -c .)" -eq 1
manifest=$(sudo docker exec --user 568:568 "$backup_id" /opt/powermeter/backup.sh)
case "$manifest" in /backups/archives/*.dump.gpg.manifest.json) ;; *) exit 1 ;; esac
archive=${manifest%.manifest.json}
sudo docker exec --user 568:568 "$backup_id" /opt/powermeter/restore.sh \
  --archive "$archive" --test-isolated | jq -e '.state == "verified"'
for evidence in last-backup-attempt last-successful-backup \
  last-restore-test-attempt last-successful-restore-test; do
  sudo jq -e '.state == "verified" and (.run_id | length > 0) and
    (.sha256 | test("^[0-9a-f]{64}$"))' \
    "/mnt/Apps/PowerMeterV2/backups/status/${evidence}.json"
done
```

Record the returned run IDs, completion timestamps, archive SHA-256, migration
revision, and table count. File existence alone is not restore evidence. See
[BACKUPS_AND_RESTORE.md](BACKUPS_AND_RESTORE.md) for operator-directed recovery
into a new database. The file is included in each release asset set and also
lives under `docs/` in the source repository.

## 8. Final network and safety boundary

Only gateway TCP 8443 is published. PostgreSQL has no host port and uses an
internal Docker network. Browser code communicates only with the same-origin
gateway. Sensors make outbound authenticated HTTPS requests only.

The application is monitoring-only and contains no relay, contactor, or load
control. Mains wiring and CT installation must be de-energized and completed by
a qualified person under the equipment instructions and local code. Do not
claim the sensor is physically certified unless the exact marked unit and
firmware have a passed machine-readable HIL/72-hour record.

After validation, remove the temporary public release directory from `/tmp`
and disable SSH if it was enabled only for installation. Do not remove the
offline backup key, CA recovery material, release manifest, or verified YAML.

For future changes, use [UPGRADE.md](UPGRADE.md) and [ROLLBACK.md](ROLLBACK.md).
Never update only an image tag or use TrueNAS's generic image-update indicator
for this digest-pinned app.
