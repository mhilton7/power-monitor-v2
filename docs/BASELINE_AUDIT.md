# Baseline and reference audit

Recorded before V2 implementation on 2026-08-13.

| Repository | Path | Branch/commit/tag | Remotes | Worktree baseline |
|---|---|---|---|---|
| V2 server target | `E:\Documents\ChatGPT\PowerMonitorV2` | `codex/greenfield-power-meter-v2`, `8b5c80b4959f3b959fd1879125938182397e39a7`, no tag | none configured | only untracked `.gitignore` and `AGENTS.md`; no application/test baseline existed |
| legacy server reference | `E:\Documents\Codex\power-monitor` | `main`, `df581522266227b0258c3303b551a7f6ec2e5362`, `v1.0.52` | `origin=https://github.com/mhilton7/power-monitor.git` | clean |
| existing firmware reference/target | `E:\Documents\Codex\power-monitor-sensor-headless` | `main`, `7a8ef4acca91c4be6d1deb6b26838f5d17616c87`, `v2.0.0` | `origin=https://github.com/mhilton7/power-monitor-sensor.git` | pre-existing 9 modified and 2 untracked files; preserved |

The firmware changes observed were `pm_agent_transport.hpp`, `agent_transport.cpp`, `runtime.cpp`, `sdkconfig.defaults`, `test_agent.py`, `test/vectors/SHA256SUMS.json`, `check_minimal_project.py`, `generate_contract_vectors.py`, `run_host_tests.py`, plus two untracked agent-heartbeat hardware vector JSON files. This audit does not attribute or alter them.

The empty target had no feasible tests/builds. The legacy server's current tests were not run because system Python lacked pytest/dependencies and installing into the read-only reference checkout would alter it. Its retained historical audit recorded **342 passed and one expected opt-in skip**; that historical result was not treated as a current pass. Firmware build/tests were not run because the worktree was already dirty/divergent and user changes had to be preserved. No other repositories were found, and hardware checkout/flash backups were not inspected. V2 test evidence begins with its own clean implementation and is recorded by CI/release reports.

## Post-baseline target status

Implementation created the independent target at
`E:\Documents\ChatGPT\PowerMonitorV2\power-monitor-sensor-headless` without
changing the preserved legacy worktree above. Its current local branch is
`codex/greenfield-headless-agent` at
`5dea90d91ecd5731b4286a5f67117741aa2ce539`; it is clean and has no remote or
tag. The coordinated server target remains on
`codex/greenfield-power-meter-v2`. Publication state and exact local evidence
are recorded in `docs/RELEASE_PROCESS.md` and `docs/TESTING.md`.

Legacy inspection identified a separate V1 dataset root, a broad model with bill/usage/reconciliation features, and historical operational patterns. V2 uses isolated `/mnt/Apps/PowerMeterV2/...` paths and rejects blind data/code migration. See `docs/MIGRATION.md`.
