# Secrets and local TLS

Create secrets on TrueNAS; never put their values in Compose YAML, `.env`,
screenshots, command arguments, shell output, Git, diagnostics, or ordinary
backups. Each file contains one value. Keep the `secrets` dataset encrypted and
unshared.

## Required files

All files live in `/mnt/Apps/PowerMeterV2/secrets`, have owner `root:root`, no
owning-group or other access, and only the listed POSIX named readers.

| File | Required value | Named read ACL | Purpose |
|---|---|---|---|
| `postgres_bootstrap_password` | 32 random bytes as 64 lowercase hex characters | UID `70` | empty-cluster bootstrap only; role becomes `NOLOGIN` |
| `postgres_migrator_password` | same | UIDs `70`, `10001` | schema ownership and Alembic only |
| `postgres_api_password` | same | UIDs `70`, `10001` | API runtime DML only |
| `postgres_worker_password` | same | UIDs `70`, `10001` | worker runtime DML only |
| `postgres_backup_password` | same | UIDs `70`, `568` | read-only logical backup |
| `postgres_restore_password` | same | UIDs `70`, `568` | isolated restore database creation only |
| `session_secret` | exactly 32 random bytes as Base64 | UID `10001` | server-side session authentication |
| `field_encryption_key` | exactly 32 random bytes as Base64 | UID `10001` | encrypted device credentials |
| `ota_manifest_key` | exactly 32 random bytes as Base64 | UID `10001` | device-specific OTA manifest authentication |
| `backup_encryption_key` | 32 or more random bytes as Base64, or six or more Diceware words | UID `568` | OpenPGP symmetric backup encryption |
| `tls.crt` | PEM leaf certificate (plus intermediates when applicable) | UID `1000` | HTTPS certificate |
| `tls.key` | matching PEM private key | UID `1000` | HTTPS key |
| `tls-ca.crt` | PEM trust anchor | UID `1000` | gateway health check and sensor/browser provisioning |

Docker Compose local-file secrets are bind mounts. The host ACL is the actual
permission boundary; Compose `uid`, `gid`, and `mode` fields are additional
metadata and must not be treated as a substitute.

## Generate the application secrets

The shortest safe path is to generate them directly in the unlocked TrueNAS
dataset. From **System > Shell**, become root, confirm the path, and run this
block once. It refuses to overwrite any existing secret:

```sh
sudo -i
base=/mnt/Apps/PowerMeterV2/secrets
test "$(realpath "$base")" = /mnt/Apps/PowerMeterV2/secrets
umask 077
write_new() {
  target=$1
  shift
  test ! -e "$target" || { printf 'refusing to overwrite %s\n' "$target" >&2; return 1; }
  "$@" >"$target"
}
for name in bootstrap migrator api worker backup restore; do
  write_new "$base/postgres_${name}_password" openssl rand -hex 32
done
for name in session_secret field_encryption_key ota_manifest_key backup_encryption_key; do
  write_new "$base/$name" openssl rand -base64 32
done
exit
```

The commands place no secret value in shell history or terminal output. Copy
`backup_encryption_key` to an offline encrypted password vault through an
authenticated administrative channel. Loss of that key makes all corresponding
backups unrecoverable. Rotating it does not re-encrypt old archives; retain old
key versions until their archives expire.

If secrets are generated on another workstation instead, transfer them without
opening an SMB/NFS share on the application datasets, securely remove the
transfer copy, and do not reuse development values.

## Create or obtain the HTTPS certificate

Use an existing internal CA when available. The certificate SAN must contain
the exact configured hostname (default `power-monitor.home.arpa`), and every
browser and sensor must trust the corresponding CA. Caddy has no automatic
public-ACME path for this LAN deployment.

This OpenSSL example creates a private root and a directly signed LAN leaf on a
trusted administrative workstation. Protect `pm-root-ca.key` offline and never
copy it to TrueNAS:

```sh
mkdir powermeter-private-ca
chmod 0700 powermeter-private-ca
cd powermeter-private-ca
umask 077
hostname=power-monitor.home.arpa
openssl genpkey -algorithm RSA -pkeyopt rsa_keygen_bits:3072 -out pm-root-ca.key
openssl req -x509 -new -sha256 -days 3650 -key pm-root-ca.key \
  -subj '/CN=PowerMeter V2 Private Root CA' \
  -addext 'basicConstraints=critical,CA:TRUE,pathlen:0' \
  -addext 'keyUsage=critical,keyCertSign,cRLSign' \
  -out tls-ca.crt
openssl genpkey -algorithm RSA -pkeyopt rsa_keygen_bits:3072 -out tls.key
openssl req -new -sha256 -key tls.key -subj "/CN=$hostname" \
  -addext "subjectAltName=DNS:$hostname" \
  -addext 'keyUsage=critical,digitalSignature,keyEncipherment' \
  -addext 'extendedKeyUsage=serverAuth' -out tls.csr
openssl x509 -req -sha256 -days 397 -in tls.csr -CA tls-ca.crt \
  -CAkey pm-root-ca.key -CAcreateserial -copy_extensions copy -out tls.crt
openssl verify -x509_strict -purpose sslserver -CAfile tls-ca.crt \
  -verify_hostname "$hostname" tls.crt
```

Transfer only `tls.crt`, `tls.key`, and `tls-ca.crt` to the TrueNAS secrets
directory. Delete `tls.csr` after issuance; retain the CA certificate and root
key according to the CA's backup policy. Install `tls-ca.crt` into each
administrative workstation's trust store only after independently comparing
its SHA-256 fingerprint. Provision the same CA certificate to firmware through
the documented USB flow; never disable chain or hostname verification.

Do not use an IP address with a hostname-only certificate. If the deployment
hostname changes, issue a new leaf, update DNS, rerender/redeploy the supported
configuration, and reprovision affected sensors.

## Apply and verify access

Run the release asset `prepare-host.sh` after all 13 files exist. It verifies
formats, certificate hostname/expiry/chain/key match, asset checksums, and exact
named ACLs before reporting success:

```sh
sudo bash ./prepare-host.sh --assets "$PWD" --hostname power-monitor.home.arpa
```

For a read-only audit that does not print values:

```sh
base=/mnt/Apps/PowerMeterV2/secrets
stat -c '%n %u:%g %a %s-bytes' "$base"/*
getfacl --absolute-names "$base"/*
openssl x509 -in "$base/tls.crt" -noout -subject -issuer -dates -ext subjectAltName
openssl verify -x509_strict -purpose sslserver -CAfile "$base/tls-ca.crt" \
  -verify_hostname power-monitor.home.arpa "$base/tls.crt"
```

Stop if the dataset is not POSIX ACL, a secret is a symbolic link, any
unlisted identity can read a file, or certificate validation fails. Fix the
dataset through the TrueNAS ACL editor; never compensate with `0777`, a shared
database password, inline secrets, or disabled TLS verification.
