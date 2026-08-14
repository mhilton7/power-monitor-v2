# Secrets and local TLS

Create secrets on TrueNAS; never put their values in Compose YAML, `.env`, screenshots, shell history, Git, diagnostics, or ordinary backups. Store one value per file without trailing commentary.

Required files in `/mnt/Apps/PowerMeterV2/secrets`:

| File | Minimum | Named read ACL | Purpose |
|---|---|---|---|
| `postgres_bootstrap_password` | 32 random bytes encoded as 64 hex characters | UID `70` | empty-cluster bootstrap only; role becomes `NOLOGIN` |
| `postgres_migrator_password` | 32 random bytes encoded as 64 hex characters | UIDs `70`, `10001` | schema ownership and Alembic only |
| `postgres_api_password` | 32 random bytes encoded as 64 hex characters | UIDs `70`, `10001` | API runtime DML only |
| `postgres_worker_password` | 32 random bytes encoded as 64 hex characters | UIDs `70`, `10001` | worker runtime DML only |
| `postgres_backup_password` | 32 random bytes encoded as 64 hex characters | UIDs `70`, `568` | read-only logical backup |
| `postgres_restore_password` | 32 random bytes encoded as 64 hex characters | UIDs `70`, `568` | isolated restore-test database creation only |
| `session_secret` | 32 random bytes encoded as Base64 | UID `10001` | server-side session authentication |
| `field_encryption_key` | exactly 32 random bytes encoded as Base64 | UID `10001` | encrypted device credentials |
| `ota_manifest_key` | 32 random bytes encoded as Base64 | UID `10001` | device-specific OTA manifest authentication |
| `backup_encryption_key` | six or more Diceware words or 32 random Base64 bytes | UID `568` | OpenPGP symmetric backup encryption |
| `tls.crt` | PEM leaf plus intermediates | UID `1000` | HTTPS certificate |
| `tls.key` | PEM private key | UID `1000` | HTTPS key |
| `tls-ca.crt` | PEM local root | UID `1000` | gateway health check and sensor provisioning |

Every file has baseline owner `root:root`, mode `0400`, plus only the named
read ACL entries above. Docker Compose implements local file secrets as bind
mounts and may not implement the `uid`, `gid`, and `mode` attributes in long
secret syntax. The host ACL is therefore authoritative; the Compose attributes
are defense-in-depth metadata, not the permission boundary.

Generate application secrets on a trusted administrative machine. These PowerShell commands write directly to files without displaying the value:

```powershell
$secretRoot = 'C:\Secure\PowerMeterV2'
New-Item -ItemType Directory -Force -Path $secretRoot | Out-Null
$rng = [System.Security.Cryptography.RandomNumberGenerator]::Create()
function Write-RandomBase64([string]$Path) {
  $bytes = New-Object byte[] 32
  $rng.GetBytes($bytes)
  [IO.File]::WriteAllText($Path, [Convert]::ToBase64String($bytes), [Text.UTF8Encoding]::new($false))
}
Write-RandomBase64 "$secretRoot\session_secret"
Write-RandomBase64 "$secretRoot\field_encryption_key"
Write-RandomBase64 "$secretRoot\ota_manifest_key"
Write-RandomBase64 "$secretRoot\backup_encryption_key"
foreach ($name in @('postgres_bootstrap_password', 'postgres_migrator_password', 'postgres_api_password', 'postgres_worker_password', 'postgres_backup_password', 'postgres_restore_password')) {
  $bytes = New-Object byte[] 32
  $rng.GetBytes($bytes)
  [IO.File]::WriteAllText("$secretRoot\$name", [Convert]::ToHexString($bytes).ToLowerInvariant(), [Text.UTF8Encoding]::new($false))
}
$rng.Dispose()
```

Transfer the files through an authenticated administrative channel, set the ownership/modes above, then securely remove the transfer copy. Keep an offline encrypted copy of `backup_encryption_key`; without it, backups cannot be restored. Rotating that key does not re-encrypt old archives, so retain old keys under documented key IDs until associated archives expire.

Apply and verify POSIX named ACLs after transferring the files. Stop if the
dataset is configured for an incompatible ACL type; convert it through the
TrueNAS dataset ACL editor rather than weakening file modes.

```sh
base=/mnt/Apps/PowerMeterV2/secrets
chown root:root "$base"/*
chmod 0400 "$base"/*
setfacl -b "$base"/*
setfacl -m u:70:r "$base/postgres_bootstrap_password"
setfacl -m u:70:r,u:10001:r "$base/postgres_migrator_password" "$base/postgres_api_password" "$base/postgres_worker_password"
setfacl -m u:70:r,u:568:r "$base/postgres_backup_password" "$base/postgres_restore_password"
setfacl -m u:10001:r "$base/session_secret" "$base/field_encryption_key" "$base/ota_manifest_key"
setfacl -m u:568:r "$base/backup_encryption_key"
setfacl -m u:1000:r "$base/tls.crt" "$base/tls.key" "$base/tls-ca.crt"
getfacl --absolute-names "$base"/*
```

Use a locally trusted CA for `power-monitor.home.arpa`. The certificate SAN must contain the exact hostname, all browsers and sensors must trust the CA, and the DNS name must resolve to the TrueNAS host. Do not disable hostname or chain verification and do not use an IP address with a hostname-only certificate. Caddy intentionally has no automatic public ACME path in this LAN deployment.

Verify metadata without printing contents:

```sh
base=/mnt/Apps/PowerMeterV2/secrets
stat -c '%n %U:%G %a %s bytes' "$base"/*
getfacl --absolute-names "$base"/*
openssl x509 -in "$base/tls.crt" -noout -subject -issuer -dates -ext subjectAltName
openssl verify -CAfile "$base/tls-ca.crt" "$base/tls.crt"
```
