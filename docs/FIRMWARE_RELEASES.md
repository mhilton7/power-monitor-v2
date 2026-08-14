# Firmware releases

Firmware is built and released from the independent `power-monitor-sensor-headless` repository, not from this server repository. Compatibility is coordinated through `pm-protocol/1.0.0`, project/target/board/config/storage versions, and cross-linked release manifests.

The server stores immutable firmware release metadata: semantic version/build, project, target chip, board profile, minimum boot/config/protocol versions, image size/SHA-256, release notes, signed provenance/SBOM/checksums, hardware-certification status, and source release URL. It never treats a filename or version string as sufficient evidence.

An administrator with `firmware.manage` selects compatible devices or a staged rollout. Each device receives a per-device signed/HMAC-authenticated manifest through its outbound heartbeat, downloads through authenticated HTTPS with bounded resume/restart, writes only the inactive OTA slot, verifies all compatibility metadata and SHA-256, reads back boot selection, and reboots. The server completes deployment only after a healthy version heartbeat and subsequent reading evidence.

Interrupted/partial/hash-mismatched/incompatible images never boot. Post-boot validation checks project/target/config/scheduler/watchdog while allowing temporary server, Wi-Fi, PZEM, SD, or SCE unavailability. Boot-loop/crash evidence triggers rollback and reports the original deployment/command ID.

Required firmware release assets include firmware/merged-flash/bootloader/partition/ELF/map binaries, flash arguments, manifest, SHA256SUMS, SBOM, provenance, memory/stack/test reports, release/migration notes, hardware certification, and PowerShell flash/provision tools. The server rejects a purported production release without required metadata.

Until machine-readable results from the actual marked ESP32-S3/PZEM/SD unit pass the hardware-in-loop suite and 72-hour soak, firmware and coordinated server releases remain prerelease candidates. Simulation is not physical certification.

## Current local release candidate

The independently buildable firmware repository is at commit
`5dea90d91ecd5731b4286a5f67117741aa2ce539` on
`codex/greenfield-headless-agent`. Its local `0.1.0-rc.1` pack was verified with
55/55 host tests, 36/36 fault-injection cases, 63/63 production-C assertions,
63/63 ASan/UBSan assertions, and an accelerated 120-day simulation containing
10,368,000 samples and 172,800 durable intervals. Two clean ESP-IDF 6.0.2
release builds were byte-identical.

`firmware.bin` is 978,576 bytes with SHA-256
`02e0c46a0bfee4fcf35a0bf82de191bf04e69a65d387fbbdbb78e6876b6b06da`.
The 24-file local pack includes the required flash binaries, checksums,
compatibility/manifest metadata, PowerShell utilities, SBOM, provenance,
dependency, memory, stack, test, migration, and release reports. It is not a
signed tag or public GitHub Release. Its manifest and hardware-certification
record remain `pending`; marked-unit identity/electrical evidence, TLS/HMAC,
OTA install/rollback, outage/power-cycle/USB recovery, and a continuous 72-hour
soak still block stable promotion.
