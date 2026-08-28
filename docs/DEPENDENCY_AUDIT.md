# Dependency and reference audit

## Controlled references

The legacy server at `E:\Documents\Codex\power-monitor` was inspected read-only at commit `df581522266227b0258c3303b551a7f6ec2e5362` (`v1.0.52`) for failure history, schema/deployment constraints, and prohibited migration surfaces. No application code was copied.

The existing firmware repository at `E:\Documents\Codex\power-monitor-sensor-headless` was recorded at commit `7a8ef4acca91c4be6d1deb6b26838f5d17616c87` (`v2.0.0`) with pre-existing local changes and was not modified by server deployment work. Compatibility is by `pm-protocol/1.0.0`, not shared application code.

The greenfield target firmware repository was created separately at
`E:\Documents\ChatGPT\PowerMonitorV2\power-monitor-sensor-headless`. Its local
candidate is commit `5dea90d91ecd5731b4286a5f67117741aa2ce539`; the legacy
repository above remains a controlled reference and is not its code base.

The official SCE public TOU page was fetched on 2026-08-13; evidence and current observed values are in `docs/SCE_REFERENCE_SNAPSHOT.md`. It is a mutable rate-source reference, not usage or tariff-effective-date evidence.

## Production infrastructure pins

| Component | Pin | Purpose/evidence |
|---|---|---|
| PostgreSQL | `17.10-alpine3.23@sha256:8189a1f6e40904781fc9e2612687877791d21679866db58b1de996b31fc312e4` | database and backup client base; Docker Official Image manifest resolved 2026-08-13 |
| Gateway Go builder | `golang:1.26.6-alpine3.23@sha256:5978cc992ad5ef96a7469713c8af849c1433824761ce3be2c56381403cd8d9a3` | exact builder; `GOTOOLCHAIN=local`, read-only modules, tidy-diff, empty build ID, trimpath, and byte-for-byte double-build comparison are enforced |
| Caddy source/runtime | source `v2.11.4` at commit `e2eee6a7fce366321294c9c2a79f3146891dcbdf`, module sum `h1:XKxkMTgNSizEvKG6QHue6cAsFOteU2qA61w2tKkCWi0=`; runtime `2.11.4-alpine@sha256:5f5c8640aae01df9654968d946d8f1a56c497f1dd5c5cda4cf95ab7c14d58648` | project-owned standard-module-only TLS gateway; rebuilt with `x/mod 0.40.0`, `x/net 0.58.0`, `x/text 0.41.0`, and `grpc 1.82.1`, then the unused privileged-port file capability is removed |
| Gateway Alpine packages | c-ares `1.34.8-r0`, curl/libcurl `8.20.0-r0` | exact v3.23 security-fixed packages installed in the final runtime; their availability is version-pinned but depends on the live Alpine v3.23 repository, so only the compiled Go binary is claimed byte-reproducible |
| Alpine OpenSSL runtime | `openssl`, `libcrypto3`, and `libssl3` `3.5.8-r0` | exact security-fixed package revision required by the API, frontend, and backup final images after CVE-2026-14456 was published; tagged CI fails closed if the pinned revision is unavailable or a HIGH/CRITICAL finding remains |
| Alpine backup packages | bash `5.3.3-r1`, coreutils `9.8-r1`, findutils `4.10.0-r0`, GnuPG `2.4.9-r0`, jq `1.8.1-r0`, tzdata `2026c-r0` | exact v3.23 repository metadata checked 2026-08-13 |
| Trivy | action `v0.35.0` at `57a97c7e7821a5776cebc9bb87c984fa69cba8f1`; scanner `v0.72.0` | release, image, filesystem, secret, and misconfiguration gates |
| actionlint | `v1.7.12`, checksum-verified release binary | local validation of every workflow; no findings on 2026-08-13 |

Application Python and npm dependency pins and their licenses are declared in `pyproject.toml` and `frontend/package-lock.json`. Release CI generates CycloneDX/SPDX SBOMs and audit reports from the clean tagged checkout and each final image; the GitHub release, not this narrative file, is authoritative for a particular build.

The 2026-08-13 local Python audit identified two 2026 PDF
resource-consumption advisories affecting the initially resolved `pypdf 6.14.2`
and a local-temporary-directory advisory affecting the initially resolved
development-only `pytest 8.4.1`. No release was published from those pins.
Resolution was upgraded to `pypdf 6.16.0`, `pytest 9.1.1`, and
`pytest-asyncio 1.4.0`; the final portable suite collected 105 tests with 101
passing and four expected environment skips, and the role-separated PostgreSQL
suite completed 102 passes with three expected skips.
The exact fully pinned production `backend/requirements.lock` then passed:

```sh
python -m pip_audit --no-deps --disable-pip -r backend/requirements.lock
```

Result: **No known vulnerabilities found** for that lock at audit time. The
frontend lock also returned zero findings from `npm audit`. These are local
time-bounded results; the tagged release reruns audits and image/filesystem
scans and publishes the checksum-backed reports.

All third-party GitHub actions are referenced by full 40-character commit SHA
with a human-readable version comment. Dependabot proposes action, pip, npm,
Docker updates for the backend, frontend, gateway, and backup build roots, and
Go module updates for the gateway; it never auto-merges them. Updates require
the same test/security/release gates.

No legacy library, UI shell, task implementation, web server, TLS ownership logic, memory policy, OTA flow, generated directory, binary, secret, NVS/database dump, certificate, or bill document is a dependency of V2.
