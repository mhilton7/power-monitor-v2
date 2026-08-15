# PowerMeter V2 on TrueNAS: UI + SMB installation

This is the supported normal installation path. It uses Windows, one temporary
authenticated SMB share, and the TrueNAS web UI. It does **not** require SSH,
the TrueNAS shell, a container console, or a host-side preparation command.

> **Release boundary:** this no-shell procedure applies to the complete signed
> v0.1.0-rc.5 release asset set and later releases that retain this contract.
> The immutable rc.3 assets use their attached rc.3 instructions and do not
> contain this initializer/staging helper. The signed server rc.4 tag has no
> GitHub Release or YAML. Never combine files from releases.

The signed release YAML runs eight services. `initialize` first validates the
staged inputs, installs the image-embedded Caddy/PostgreSQL configuration, and
repairs/verifies the exact runtime permissions. It exits successfully before
`postgres`, `migrate`, or any long-running service can start. `migrate` is the
second one-shot service; the other six services remain running.
Compatible sensors use `pm-protocol/1.0.0`; live/history/usage evidence remains
authenticated PZEM-004T readings only.
Bill uploads contribute closed-schema reusable rate facts only. Original PDF
bytes and full OCR text are released after the bounded parse and are never
stored, encrypted or otherwise; this release intentionally has no bill-original
dataset.

## 1. Verify the public release on Windows

Install GitHub CLI and Git for Windows, sign in with `gh auth login`, then use
the exact coordinated release tag. A unique directory below Windows `%TEMP%`
prevents an earlier partial download from contaminating this asset set:

```powershell
$Tag = 'v0.1.0-rc.5'
$TempRoot = [IO.Path]::GetFullPath($env:TEMP)
$Release = Join-Path $TempRoot ("powermeter-{0}-{1}" -f $Tag, [guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $Release -ErrorAction Stop | Out-Null
gh release download $Tag --repo mhilton7/power-monitor-v2 --dir $Release
$Sums = Join-Path $Release 'SHA256SUMS'
$Expected = @{}
foreach ($Line in Get-Content -LiteralPath $Sums) {
    if ($Line -cnotmatch '^([0-9a-f]{64}) [ *]([A-Za-z0-9][A-Za-z0-9._-]*)$') {
        throw 'Malformed SHA256SUMS line'
    }
    if ($Expected.ContainsKey($Matches[2])) { throw 'Duplicate SHA256SUMS asset' }
    $Expected[$Matches[2]] = $Matches[1]
}
$Downloaded = @(Get-ChildItem -LiteralPath $Release -File |
    Where-Object Name -cne 'SHA256SUMS' | ForEach-Object Name | Sort-Object)
$Listed = @($Expected.Keys | Sort-Object)
if (Compare-Object $Listed $Downloaded) { throw 'Release asset set mismatch' }
foreach ($Asset in $Listed) {
    $Path = Join-Path $Release $Asset
    $Actual = (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash.ToLowerInvariant()
    if ($Actual -cne $Expected[$Asset]) { throw "SHA-256 mismatch: $Asset" }
    gh attestation verify $Path --repo mhilton7/power-monitor-v2
    if ($LASTEXITCODE -ne 0) { throw "Attestation failed: $Asset" }
}
```

This requires the exact listed asset set and verifies every SHA-256 and
attestation. Stop on any failure. Never replace an image digest or use `latest`.

## 2. Create the fixed ZFS datasets in the TrueNAS UI

In **Datasets**, create `Apps/PowerMeterV2` as a Generic, case-sensitive ZFS
dataset with POSIX ACLs. Under it, create these nine child ZFS datasets with the
same settings:

1. `postgres`
2. `config`
3. `firmware`
4. `backups`
5. `logs`
6. `rate-source-artifacts`
7. `caddy-data`
8. `caddy-config`
9. `secrets`

The resulting fixed root is `/mnt/Apps/PowerMeterV2`. Each child must be a real
ZFS dataset created in the UI, not a folder. The YAML uses
`create_host_path: false`, so a missing child fails instead of being silently
created. The initializer can prove that each fixed container mount exists, but
container mount namespaces cannot prove the host-side ZFS dataset boundary;
the UI creation above is therefore a mandatory operator precondition.

Do not share the root or the other eight children. Never share `postgres`,
`backups`, `logs`, or `config`.

## 3. Temporarily stage only the secrets dataset over authenticated SMB

Create a dedicated, non-administrator TrueNAS user such as
`powermeter-stager`. In the dataset permission editor, give that user temporary
write access to **only** `Apps/PowerMeterV2/secrets`; do not apply permissions
recursively and do not alter the parent.

In **Shares > Windows (SMB) Shares**, create one temporary share:

- Path: `/mnt/Apps/PowerMeterV2/secrets`
- Name: `PowerMeterV2-Secrets`
- Guest access: disabled
- Enabled: yes only during this staging step

The local Windows source directory must contain exactly these 13 files and no
subdirectories:

```text
postgres_bootstrap_password  postgres_migrator_password
postgres_api_password        postgres_worker_password
postgres_backup_password     postgres_restore_password
session_secret               field_encryption_key
ota_manifest_key             backup_encryption_key
tls.crt                      tls.key
tls-ca.crt
```

From the verified release directory run:

```powershell
$Credential = Get-Credential -Message 'Dedicated PowerMeter SMB staging user'
& .\Stage-PowerMeterTrueNAS.ps1 `
    -SourceDirectory 'C:\PowerMeterV2\secrets' `
    -TrueNasAddress '192.168.0.175' `
    -ShareName 'PowerMeterV2-Secrets' `
    -HostName 'power-monitor.home.arpa' `
    -Credential $Credential
```

The helper generates or rotates nothing. It validates the exact file set,
decoded key independence, TLS hostname/expiry/strict chain/key match, performs
same-share staged copies, and verifies the final bytes without printing values
or hashes. Existing differing or partial destinations are rejected.

After its success line, immediately **disable or delete the SMB share in the
TrueNAS UI**. Confirm guest access remains disabled. Do this before installing
the app. The one-shot initializer will remove the temporary staging user's file
access and install the exact per-service numeric ACLs. No long-running service
ever receives the secrets directory; each receives only its declared files.

## 4. Configure the supported DNS name

Create a local DNS A record:

```text
power-monitor.home.arpa -> 192.168.0.175
```

The certificate must contain `power-monitor.home.arpa` in its DNS SAN. The
supported application URL is `https://power-monitor.home.arpa:8443`.
Direct-IP HTTPS is not supported by this release contract; the initializer and
gateway require the DNS hostname. Never bypass hostname or CA verification.
Trust only the staged `tls-ca.crt` in the intended workstation/user trust store.

## 5. Paste the complete signed YAML

Open **Apps > Discover Apps > Custom App > Install via YAML**. Use app name
`powermeter-v2`. Open the release file named
`power-monitor-v2-<tag>.yaml`, copy the **complete** file, paste it into the YAML
editor without changes, and save.

Do not paste a compact example, an excerpt, a Windows path, or an SMB UNC path.
The only published port must be TCP 8443. The four PowerMeter images must keep
their release tags and immutable SHA-256 digests.

In **Apps > Installed > powermeter-v2 > Workloads/Logs**, expect:

1. `initialize` exits with code 0 and reports 9 mounts, 13 preserved files,
   and two embedded configuration assets.
2. `postgres` becomes healthy.
3. `migrate` exits with code 0.
4. `api`, `worker`, `frontend`, `gateway`, and `backup` become healthy.

Any initializer or migration failure intentionally blocks all dependent
services. Correct the named input or missing dataset; never bypass the check.

## 6. Verify from Windows and complete first run

Open `https://power-monitor.home.arpa:8443`, without accepting a browser
warning. Complete the one-time owner setup. From PowerShell, with the CA trusted:

```powershell
Resolve-DnsName power-monitor.home.arpa
Test-NetConnection 192.168.0.175 -Port 8443
Invoke-RestMethod 'https://power-monitor.home.arpa:8443/healthz'
Invoke-RestMethod 'https://power-monitor.home.arpa:8443/health/live'
Invoke-RestMethod 'https://power-monitor.home.arpa:8443/health/ready'
```

Use the TrueNAS app UI for routine service status and logs. Keep the temporary
SMB share disabled. Removing the app does not authorize deletion of any
dataset; preserve the database, backup key, and off-system verified backups.

In authenticated **Settings > Backups & restore**, wait for and require both a
recent successful encrypted backup and a successful isolated restore test.
Then open
`https://power-monitor.home.arpa:8443/api/v1/backups/status` in the same
authenticated browser and save the machine-readable response. Record the
backup and restore run IDs, UTC timestamps, archive SHA-256, migration revision,
and restored table count. A present archive without successful restore evidence
is not verified. These normal verification steps use the browser and TrueNAS
Apps UI only; they do not require SSH, System Shell, or a container console.
