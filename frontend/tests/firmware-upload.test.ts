import { firmwareUpgradeAvailable, prepareFirmwareUpload } from '../src/lib/firmwareUpload';

async function sha256Hex(value: Uint8Array) {
  const buffer = new ArrayBuffer(value.byteLength);
  new Uint8Array(buffer).set(value);
  const digest = await crypto.subtle.digest('SHA-256', buffer);
  return Array.from(new Uint8Array(digest), (byte) => byte.toString(16).padStart(2, '0')).join('');
}

async function releaseFiles(overrides: Record<string, unknown> = {}) {
  const imageBytes = new TextEncoder().encode('PowerMeter signed OTA fixture');
  const imageSha256 = await sha256Hex(imageBytes);
  const manifest = {
    schema: 'pm-firmware-release/1.0.0',
    version: '0.1.0-rc.9',
    build_number: 12,
    project_name: 'power-monitor-sensor-headless',
    target_chip: 'esp32s3',
    board_profile: 'esp32-s3-devkitc-n16r8-reference/1',
    minimum_boot_version: 1,
    minimum_config_version: 1,
    minimum_protocol: 'pm-protocol/1.0.0',
    image_size: imageBytes.byteLength,
    image_sha256: imageSha256,
    download_url: 'https://github.com/mhilton7/power-monitor-sensor-headless/releases/download/v0.1.0-rc.9/firmware.bin',
    ota_authentication: {
      mode: 'per-device-hmac-sha256',
      canonical_prefix: 'PM-OTA-MANIFEST-V1',
      required_runtime_fields: ['manifest_nonce', 'signature'],
      note: 'The central server signs a fresh per-device manifest.',
    },
    hardware_certification: 'pending',
    git_commit: '0e6e268e00a16eef31ad345a6703b2a78bd154a8',
    ...overrides,
  };
  return {
    image: new File([imageBytes], 'firmware.bin', { type: 'application/octet-stream' }),
    manifest: new File([JSON.stringify(manifest)], 'manifest.json', { type: 'application/json' }),
    imageSha256,
  };
}

describe('firmware upload metadata', () => {
  it('fills every server upload field from a matching locked manifest', async () => {
    const files = await releaseFiles();
    const prepared = await prepareFirmwareUpload(files.image, files.manifest);

    expect(prepared.fields).toMatchObject({
      semantic_version: '0.1.0-rc.9',
      build_number: 12,
      board_profile: 'esp32-s3-devkitc-n16r8-reference/1',
      minimum_boot_version: 1,
      minimum_config_version: 1,
      expected_sha256: files.imageSha256,
    });
    expect(prepared.fields.release_notes).toContain('source commit 0e6e268e');
  });

  it('rejects a binary that does not match its manifest', async () => {
    const files = await releaseFiles({ image_sha256: 'a'.repeat(64) });
    await expect(prepareFirmwareUpload(files.image, files.manifest)).rejects.toThrow(/SHA-256 does not match/);
  });

  it('allows upgrades but rejects same-version and downgrade targets', () => {
    expect(firmwareUpgradeAvailable('0.1.0-rc.8', '0.1.0-rc.9')).toBe(true);
    expect(firmwareUpgradeAvailable('0.1.0-rc.9', '0.1.0-rc.9')).toBe(false);
    expect(firmwareUpgradeAvailable('0.1.0', '0.1.0-rc.9')).toBe(false);
    expect(firmwareUpgradeAvailable('unknown', '0.1.0-rc.9')).toBe(true);
  });
});
