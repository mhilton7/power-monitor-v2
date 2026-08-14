# PowerMeter V2 stable release

This stable release promotes a previously tested release candidate without
rebuilding its application images. The release manifest records the exact API,
frontend, gateway, and backup digests, compatible firmware release, and SHA-256 of the
attached marked-unit hardware certification evidence.

Stable promotion is fail-closed: it requires the public candidate release and
attestations, checksum-valid assets, public GHCR digests, a public compatible
firmware release, the firmware image attestation, schema-valid physical
certification, verified TLS chain and hostname behavior, OTA success and
rollback, and a passing soak of at least 72 hours with no unexplained reboot or
sequence regression.

Review `release-manifest.json`, `hardware-certification.json`, the test/security/
migration/deployment reports, and `SHA256SUMS` before installation. Use the
attached digest-pinned TrueNAS YAML and operator guides; never substitute a
floating image tag.
