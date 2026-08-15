# TrueNAS secret and TLS inputs

PowerMeter V2 uses exactly 13 pre-existing files. The app initializer validates
and protects them but never generates, rotates, replaces, prints, or hashes
their values. Create and retain them on a trusted Windows workstation before
the first install. Store the backup encryption key and CA private key offline;
the CA private key must never be staged on TrueNAS.

## Exact files and formats

| Files | Required value |
|---|---|
| six `postgres_*_password` files | exactly 64 lowercase hexadecimal characters (32 random bytes) |
| `session_secret`, `field_encryption_key`, `ota_manifest_key` | canonical Base64 decoding to exactly 32 independent random bytes |
| `backup_encryption_key` | canonical Base64 decoding to at least 32 random bytes, or at least six Diceware words |
| `tls.crt` | PEM leaf certificate followed by any intermediate certificates |
| `tls.key` | matching unencrypted PEM private key |
| `tls-ca.crt` | PEM trust anchor used by clients and gateway health checks |

The exact filenames are:

```text
postgres_bootstrap_password  postgres_migrator_password
postgres_api_password        postgres_worker_password
postgres_backup_password     postgres_restore_password
session_secret               field_encryption_key
ota_manifest_key             backup_encryption_key
tls.crt                      tls.key
tls-ca.crt
```

Each database/application/backup value must represent independent underlying
key bytes. Text secret files contain one ASCII value with no newline. TLS files
use normal PEM line endings. The certificate must:

- contain DNS SAN `power-monitor.home.arpa`;
- remain valid for at least seven days at installation;
- verify with strict server-purpose rules against `tls-ca.crt`;
- match `tls.key`;
- use an unencrypted private-key PEM encoding.

Never use a production certificate whose only SAN is `192.168.0.175`, and never
disable hostname or chain verification.

## Staging boundary

Follow `INSTALLATION.md` to create temporary authenticated SMB access to only
the `secrets` child dataset. Run the checksummed release asset
`Stage-PowerMeterTrueNAS.ps1` from Windows. It accepts only the exact 13-file
source and empty (or byte-identical complete) destination, uses same-share
temporary copies and per-file rename, writes its non-secret completion marker
last, verifies the final files, then removes the marker.

Disable or delete the SMB share before pasting the release YAML. During the
one-shot initialization only, the initializer sees the secrets directory so it
can reject extras and revoke the temporary SMB ACL. All 13 values are also
mounted individually and compared to their host source. Long-running services
receive only their declared individual Compose secrets.

## Recovery rules

- A partial/differing destination is a hard failure. Inspect it through the
  same restricted SMB share; do not ask the staging helper to overwrite it.
- Do not rotate database credentials by replacing files. Database roles and
  applications must change in one coordinated, tested operation.
- Do not delete `backup_encryption_key` while any encrypted backup may be
  needed. Test restore evidence before relying on a backup.
- Never include secrets, TLS private keys, database dumps, or secret hashes in
  logs, screenshots, Git, release assets, or support bundles.
