# Firmware releases

Firmware is built and released from the independent
`power-monitor-sensor-headless` repository. Compatibility is coordinated
through `pm-protocol/1.0.0`, the additive `pm-telemetry/2.0.0` contract,
project/target/board/config versions, and cross-linked release manifests.

The server stores immutable firmware release metadata: semantic version/build, project, target chip, board profile, minimum boot/config/protocol versions, image size/SHA-256, release notes, signed provenance/SBOM/checksums, hardware-certification status, and source release URL. It never treats a filename or version string as sufficient evidence.

The uploaded firmware binary is a disposable installation artifact. After every intended target has reached a terminal deployment state, an administrator may remove its server-side bytes from Settings. Removal is blocked while any deployment is staged, queued, downloading, or validating. The release identity, SHA-256, deployment outcomes, and audit evidence remain, and the removed version cannot be deployed again. A newer signed release must be uploaded for a future OTA.

For staged rollouts, target order is preserved. Exactly one sensor is queued at a time. An authenticated post-reboot heartbeat reporting the exact target semantic version completes the validating deployment at 100% and releases the next staged sensor. Repeating a deploy request cannot bypass an already-active staged rollout.

An administrator with `firmware.manage` selects compatible devices or a staged rollout. Each device receives a per-device signed/HMAC-authenticated manifest through its outbound heartbeat, downloads through authenticated HTTPS with bounded resume/restart, writes only the inactive OTA slot, verifies all compatibility metadata and SHA-256, reads back boot selection, and reboots. The server completes deployment only after a healthy version heartbeat and subsequent reading evidence.

Interrupted, partial, hash-mismatched, or incompatible images never boot.
Post-boot validation checks project, target, configuration, scheduler, watchdog,
and stateless telemetry runtime while allowing temporary server, Wi-Fi, PZEM,
or SCE unavailability. Boot-loop/crash evidence triggers rollback and reports
the original deployment/command ID.

Required firmware release assets include firmware/merged-flash/bootloader/partition/ELF/map binaries, flash arguments, manifest, SHA256SUMS, SBOM, provenance, memory/stack/test reports, release/migration notes, hardware certification, and PowerShell flash/provision tools. The server rejects a purported production release without required metadata.

Until machine-readable results from the actual marked ESP32-S3/PZEM unit pass
the hardware-in-loop suite and 72-hour soak, firmware and coordinated server
releases remain prerelease candidates. Simulation is not physical
certification.

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

## Historical rc.2 failure and coordinated public rc.3/rc.4/rc.5 firmware

The signed, public firmware
[`v0.1.0-rc.2`](https://github.com/mhilton7/power-monitor-sensor-headless/releases/tag/v0.1.0-rc.2)
prerelease is retained as historical evidence. It changed release identity and
compatible-server metadata while retaining firmware runtime behavior and
`pm-protocol/1.0.0`. Server rc.2 release run `31866197054` nevertheless failed
before server publication because firmware rc.2 declared a stale
`power-meter-v2.openapi.json` hash. No server rc.2 Release, image set, TrueNAS
YAML, or deployment smoke was produced. The signed tags are immutable and must
not be moved or relabeled.

The distinct coordinated public firmware rc.3 release names server
`v0.1.0-rc.3` and declares the generated OpenAPI SHA-256
`7caada9c6295f4c201fd7ce7d383822e6b5785a960022de8355e3b6acc9a4e2c`.
It retains `pm-protocol/1.0.0`; the server rc.3 release gate verified its public
contracts, checksums, attestations, and cross-links before publication.

The signed, public firmware
[`v0.1.0-rc.4`](https://github.com/mhilton7/power-monitor-sensor-headless/releases/tag/v0.1.0-rc.4)
is immutable historical evidence for the server rc.4 attempt. It names server
`v0.1.0-rc.4` and declares the exact generated OpenAPI SHA-256
`f9b936468f5a696a0bee3e04edda021c12ab81dddc091cbb307face0be1de7b1`.
Firmware runtime behavior and `pm-protocol/1.0.0` remain unchanged. Firmware
rc.4 was signed, published, and independently verified before the matching
server tag was created. The server rc.4 run later failed deterministic
deployment smoke, skipped assembly, and produced no server Release or YAML;
that outcome does not invalidate or relabel the firmware release.

The signed, public firmware
[`v0.1.0-rc.5`](https://github.com/mhilton7/power-monitor-sensor-headless/releases/tag/v0.1.0-rc.5)
names public server `v0.1.0-rc.5` and declares generated OpenAPI SHA-256
`66b4e1cfb0f5a5797dadd9a8783ff0b192ca416d1f4264c135a4e380b2b94591`.
The matching server rc.5 release completed publication and remains public and
installable with its attached assets.

Hardware execution subsequently confirmed that firmware rc.1 through rc.5
crash in the main stack before provisioning. Those signed releases remain
immutable evidence. Coordinated public firmware/server rc.6 delivered the
main-stack hotfix and remains an immutable installation authority.

Public firmware rc.12 carries the supported FAT capacity/full-state fix,
refreshes capacity while mounted so stale full state can recover, retries
trusted-time synchronization without changing measurement cadence, and binds
the public server rc.13 contract. Firmware rc.14 and rc.15 remain immutable
historical releases and must not be moved, rewritten, or relabeled.

Public firmware/server rc.16 and rc.20 remain immutable historical installation
evidence. Coordinated firmware rc.21 must name server `v0.1.0-rc.21`, retain
`pm-protocol/1.0.0`, declare `pm-telemetry/2.0.0`, and bind generated OpenAPI
SHA-256
`6d276b738467c867d062ab78b6cdc76d246f15d5aca7e2c505cddabf9b6f2c24`.
Public RC20 build number is 23 and remains immutable historical evidence.
RC21 firmware retains the stateless runtime, anchors successful telemetry to
fixed cadence deadlines so HTTPS latency does not create artificial missing
samples, keeps only one in-flight and one newest pending sample in RAM, and
preserves existing NVS identity/configuration through the schema-v1 layout.
Its build number is 24. Its metadata and artifacts must be created, signed,
published, and independently verified before the server rc.21 tag is created.

All firmware candidates retain hardware-certification status `pending`.
Marked-unit identity/electrical evidence, TLS/HMAC, OTA install/rollback,
outage/power-cycle/USB recovery, and a continuous 72-hour soak still block
stable promotion.
