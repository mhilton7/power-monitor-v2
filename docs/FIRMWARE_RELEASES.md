# Firmware releases

Firmware is built and released from the independent `power-monitor-sensor-headless` repository, not from this server repository. Compatibility is coordinated through `pm-protocol/1.0.0`, project/target/board/config/storage versions, and cross-linked release manifests.

The server stores immutable firmware release metadata: semantic version/build, project, target chip, board profile, minimum boot/config/protocol versions, image size/SHA-256, release notes, signed provenance/SBOM/checksums, hardware-certification status, and source release URL. It never treats a filename or version string as sufficient evidence.

An administrator with `firmware.manage` selects compatible devices or a staged rollout. Each device receives a per-device signed/HMAC-authenticated manifest through its outbound heartbeat, downloads through authenticated HTTPS with bounded resume/restart, writes only the inactive OTA slot, verifies all compatibility metadata and SHA-256, reads back boot selection, and reboots. The server completes deployment only after a healthy version heartbeat and subsequent reading evidence.

Interrupted/partial/hash-mismatched/incompatible images never boot. Post-boot validation checks project/target/config/scheduler/watchdog while allowing temporary server, Wi-Fi, PZEM, SD, or SCE unavailability. Boot-loop/crash evidence triggers rollback and reports the original deployment/command ID.

Required firmware release assets include firmware/merged-flash/bootloader/partition/ELF/map binaries, flash arguments, manifest, SHA256SUMS, SBOM, provenance, memory/stack/test reports, release/migration notes, hardware certification, and PowerShell flash/provision tools. The server rejects a purported production release without required metadata.

Until machine-readable results from the actual marked ESP32-S3/PZEM/SD unit pass the hardware-in-loop suite and 72-hour soak, firmware and coordinated server releases remain prerelease candidates. Simulation is not physical certification.

## Historical published v0.1.0-rc.1 evidence

The signed, public firmware
[`v0.1.0-rc.1`](https://github.com/mhilton7/power-monitor-sensor-headless/releases/tag/v0.1.0-rc.1)
prerelease is the historical firmware paired with the published server rc.1
candidate. The following values preserve its prepublication development
snapshot and belong only to rc.1; they must not be relabeled as later-release
evidence.

The independently buildable firmware repository was validated at commit
`5dea90d91ecd5731b4286a5f67117741aa2ce539` on
`codex/greenfield-headless-agent`. Its rc.1 snapshot was verified with
55/55 host tests, 36/36 fault-injection cases, 63/63 production-C assertions,
63/63 ASan/UBSan assertions, and an accelerated 120-day simulation containing
10,368,000 samples and 172,800 durable intervals. Two clean ESP-IDF 6.0.2
release builds were byte-identical.

That snapshot's `firmware.bin` is 978,576 bytes with SHA-256
`02e0c46a0bfee4fcf35a0bf82de191bf04e69a65d387fbbdbb78e6876b6b06da`.
The 24-file local pack includes the required flash binaries, checksums,
compatibility/manifest metadata, PowerShell utilities, SBOM, provenance,
dependency, memory, stack, test, migration, and release reports. These preserved
local values are historical test evidence, not substitutes for the public
rc.1 release's attached `SHA256SUMS`, attestations, or downloadable assets.

## Historical rc.2 and current coordinated target

The signed, public firmware
[`v0.1.0-rc.2`](https://github.com/mhilton7/power-monitor-sensor-headless/releases/tag/v0.1.0-rc.2)
prerelease is retained as historical evidence. It changed release identity and
compatible-server metadata while retaining firmware runtime behavior and
`pm-protocol/1.0.0`. Server rc.2 release run `31866197054` nevertheless failed
before server publication because firmware rc.2 declared a stale
`power-meter-v2.openapi.json` hash. No server rc.2 Release, image set, TrueNAS
YAML, or deployment smoke was produced. The signed tags are immutable and must
not be moved or relabeled.

The current server rc.3 coordination target is a distinct firmware rc.3 release
that names server `v0.1.0-rc.3` and declares the generated OpenAPI SHA-256
`7caada9c6295f4c201fd7ce7d383822e6b5785a960022de8355e3b6acc9a4e2c`.
It must retain `pm-protocol/1.0.0`. The server release audit must verify that
public firmware rc.3 release, its contracts, checksums, attestations, and
cross-links before publishing server assets. This document does not copy rc.1
or rc.2 test totals or asset hashes onto rc.3 and does not invent final rc.3
asset values before that audit completes.

All firmware candidates retain hardware-certification status `pending`.
Marked-unit identity/electrical evidence, TLS/HMAC, OTA install/rollback,
outage/power-cycle/USB recovery, and a continuous 72-hour soak still block
stable promotion.
