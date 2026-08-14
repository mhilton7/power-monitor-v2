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
| Caddy | `2.11.4-alpine@sha256:5f5c8640aae01df9654968d946d8f1a56c497f1dd5c5cda4cf95ab7c14d58648` | TLS gateway; Docker Official Image manifest resolved 2026-08-13 |
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

All third-party GitHub actions are referenced by full 40-character commit SHA with a human-readable version comment. Dependabot proposes action, pip, and npm updates; it never auto-merges them. Updates require the same test/security/release gates.

No legacy library, UI shell, task implementation, web server, TLS ownership logic, memory policy, OTA flow, generated directory, binary, secret, NVS/database dump, certificate, or bill document is a dependency of V2.
