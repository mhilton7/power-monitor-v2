# Security

PowerMeter V2 assumes the LAN is not inherently trusted. TLS, authentication, authorization, replay protection, input limits, auditability, and least privilege remain enabled in production.

## Controls

- Caddy exposes only HTTPS 8443 using an operator-provided, hostname-valid certificate. HSTS and restrictive CSP/COOP/CORP/Permissions/Referrer/content-type/frame headers are set. There is no HTTP listener or insecure TLS fallback.
- Browser sessions are server-side with Secure/HttpOnly/SameSite cookies, CSRF tokens, optional TOTP MFA, expiry/revocation, login throttling, and protected last-owner semantics.
- Devices use directional HKDF/HMAC credentials, exact body hashes, canonical queries, timestamp windows, nonce replay rejection, constant-time comparison, rotation, and immediate revocation. Device secrets never reach browsers.
- API input/output models are closed; body/upload/page/decompression/memory/CPU/time bounds precede expensive parsing. Errors use RFC 9457 and redact values.
- SCE fetches are HTTPS/host allowlisted with redirect, DNS rebinding, non-public IP, timeout, and size defenses. Browser and firmware never scrape sources.
- Bill-rate processing is isolated and local. A pre-parser launcher enforces Landlock ABI 3+ filesystem confinement, a seccomp network/dangerous-syscall deny policy, no-new-privileges, no dumps, closed file descriptors, a cleared fixed environment, rlimits/deadline/output caps, and a private per-job directory on hardened tmpfs. Only the frozen parser runtime/code, fonts, and Tesseract data are readable; API code/data, `/run/secrets`, DB paths, and socket creation are denied. Only closed-schema reusable rate fields escape. Original PDF bytes and full OCR text are released after the bounded parse and are never persisted, encrypted or otherwise; they are also prohibited from logs, backups, exports, diagnostics, and browser persistence.
- Database constraints/transactions enforce immutable readings, sequence uniqueness/conflict detection, cost lineage, immutable used rate versions, and append-only audit data. PostgreSQL uses separate bootstrap, schema-owner, API, worker, read-only backup, and isolated-restore credentials; no runtime service mounts the bootstrap or migrator secret, and only the isolated restore identity has `CREATEDB`.
- Production containers use immutable digests, non-root identities where supported, read-only root filesystems, no-new-privileges, all capabilities dropped, bounded resources, file secrets, internal database networking, and no Docker socket/privileged mode. The project-owned gateway rebuilds Caddy 2.11.4 with Go 1.26.6 and the exact security-fixed `x/mod 0.40.0`, `x/net 0.58.0`, `x/text 0.41.0`, and `grpc 1.82.1` module floors; it upgrades only the affected pinned Alpine runtime packages. It then removes Caddy's inherited `cap_net_bind_service` file capability because HTTPS listens on unprivileged port 8443. This keeps Caddy executable as UID 1000 while preserving the all-capabilities-dropped and no-new-privileges boundary. The strict Trivy HIGH/CRITICAL gate has no ignore or unfixed-vulnerability exemption.
- Tagged builds use a clean checkout, minimally scoped `GITHUB_TOKEN`, immutable action SHAs, dependency/secret/static/container scanning, SBOMs, and Sigstore/GitHub attestations. Dependabot changes are never auto-merged without gates.

## Sensitive data

Never commit or expose passwords, tokens, cookies, device/OTA/encryption keys, TLS private material, bill PDFs/OCR, customer identity, NVS/database dumps, or production logs. Diagnostic/log schemas are allowlists, not best-effort regex redaction. `scripts/validate_release.py`, CodeQL, dependency review, pip/npm audits, Gitleaks, and container scanning form release gates.

## Threat-specific behavior

- Replay/tamper: reject nonce reuse, stale time, wrong body hash/signature, or different content at an existing sequence.
- Privilege escalation: enforce permission and home/device ownership at service boundaries; destructive operations require typed prepare/commit.
- SSRF: reject arbitrary schemes/hosts, credentials, non-public/resolved-rebound IPs, and unvalidated redirects.
- Upload bomb/parser exploit: verify signature/MIME, cap size/pages/decompression/resources/deadline/stdout, parse only after the kernel sandbox self-test succeeds, validate closed-schema stdout and source lineage, destroy per-job tmpfs, and fail closed without a production fallback.
- Supply chain: exact dependencies/images/actions, source/revision OCI labels, digest manifests, SBOM/provenance, no floating production tags.
- Backup theft: encrypted archives, separately protected key, ciphertext/plaintext verification, restricted dataset ACL.
- Secret/log disclosure: structured allowlisted events, automatic retention, redacted support bundles, no raw request headers/bodies.

Report vulnerabilities privately through the repository's GitHub Security Advisory flow. Do not put credentials, customer documents, or exploit-sensitive production data in a public issue.

## Local security evidence and limits

The 2026-08-14 local candidate produced the following bounded evidence:

- the production API image returned
  `{"pdf_sandbox":"enforced","schema_id":"pm-pdf-sandbox-health/1.0.0"}`;
- the real PostgreSQL role-split suite proved API/worker DML with DDL denied,
  backup writes denied, restore access to the production database denied, and
  the bootstrap identity set to `NOLOGIN`;
- the fully pinned production Python lock and frontend npm lock reported zero
  known vulnerabilities at audit time, and the firmware OSV audit reported
  zero vulnerabilities;
- final local containers ran as their declared non-root users with read-only
  roots, all capabilities dropped, `no-new-privileges`, and restricted tmpfs
  paths; the rebuilt gateway's amd64 and arm64 final images each returned zero
  HIGH/CRITICAL findings from pinned Trivy 0.72.0; and
- the closed bill-rate boundary, home scoping, session throttling, credential
  rotation, diagnostics redaction, HMAC/replay, SSRF, and immutable cost/reading
  controls are exercised by the passing local suites described in
  `docs/TESTING.md`.

These are local, time-bounded results. GitHub dependency review, CodeQL,
Gitleaks, image/filesystem scanning, public-image SBOM/provenance attestation,
and target-TrueNAS runtime checks still require the signed release workflow and
remain release blockers. No local Docker image ID is a supply-chain identity.
