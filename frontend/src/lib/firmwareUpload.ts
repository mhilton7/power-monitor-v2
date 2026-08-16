import { z } from 'zod';

const sha256Schema = z.string().regex(/^[0-9a-f]{64}$/);
const semanticVersionSchema = z.string().regex(/^[0-9]+\.[0-9]+\.[0-9]+(?:-[0-9A-Za-z.-]+)?$/);

const firmwareReleaseManifestSchema = z.object({
  schema: z.literal('pm-firmware-release/1.0.0'),
  version: semanticVersionSchema,
  build_number: z.number().int().min(1).max(4_294_967_295),
  project_name: z.literal('power-monitor-sensor-headless'),
  target_chip: z.literal('esp32s3'),
  board_profile: z.literal('esp32-s3-devkitc-n16r8-reference/1'),
  minimum_boot_version: z.number().int().min(1),
  minimum_config_version: z.number().int().min(1),
  minimum_protocol: z.literal('pm-protocol/1.0.0'),
  image_size: z.number().int().min(1).max(7_864_320),
  image_sha256: sha256Schema,
  download_url: z.string().url().refine((value) => value.startsWith('https://'), 'Firmware download URL must use HTTPS.'),
  ota_authentication: z.object({
    mode: z.literal('per-device-hmac-sha256'),
    canonical_prefix: z.literal('PM-OTA-MANIFEST-V1'),
    required_runtime_fields: z.tuple([z.literal('manifest_nonce'), z.literal('signature')]),
    note: z.string().min(1),
  }).strict(),
  hardware_certification: z.enum(['pending', 'certified']),
  git_commit: z.string().regex(/^[0-9a-f]{40}$/),
}).strict();

export interface FirmwareUploadFields {
  semantic_version: string;
  build_number: number;
  board_profile: string;
  minimum_boot_version: number;
  minimum_config_version: number;
  expected_sha256: string;
  release_notes: string;
}

export interface PreparedFirmwareUpload {
  fields: FirmwareUploadFields;
  imageSize: number;
  projectName: string;
  targetChip: string;
  hardwareCertification: 'pending' | 'certified';
  gitCommit: string;
}

interface ComparableVersion {
  major: number;
  minor: number;
  patch: number;
  releaseCandidate: number | null;
}

function parseComparableVersion(value: string): ComparableVersion | null {
  const match = /^v?([0-9]+)\.([0-9]+)\.([0-9]+)(?:-rc\.([1-9][0-9]*))?$/.exec(value);
  if (!match) return null;
  const values = match.slice(1, 4).map(Number);
  const releaseCandidate = match[4] === undefined ? null : Number(match[4]);
  if (values.some((part) => !Number.isSafeInteger(part)) || (releaseCandidate !== null && !Number.isSafeInteger(releaseCandidate))) return null;
  return { major: values[0]!, minor: values[1]!, patch: values[2]!, releaseCandidate };
}

export function firmwareUpgradeAvailable(installed: string | null | undefined, candidate: string) {
  if (!installed) return true;
  const current = parseComparableVersion(installed);
  const next = parseComparableVersion(candidate);
  if (!current || !next) return true;
  for (const key of ['major', 'minor', 'patch'] as const) {
    if (next[key] !== current[key]) return next[key] > current[key];
  }
  if (next.releaseCandidate === null) return current.releaseCandidate !== null;
  if (current.releaseCandidate === null) return false;
  return next.releaseCandidate > current.releaseCandidate;
}

async function sha256Hex(file: File) {
  const digest = await crypto.subtle.digest('SHA-256', await file.arrayBuffer());
  return Array.from(new Uint8Array(digest), (value) => value.toString(16).padStart(2, '0')).join('');
}

export async function prepareFirmwareUpload(
  image: File,
  manifestFile: File,
  releaseNotesFile?: File,
): Promise<PreparedFirmwareUpload> {
  if (manifestFile.size > 65_536) throw new Error('The firmware manifest exceeds the 64 KiB safety limit.');
  let manifestJson: unknown;
  try {
    manifestJson = JSON.parse(await manifestFile.text()) as unknown;
  } catch {
    throw new Error('The firmware manifest is not valid JSON.');
  }
  const parsed = firmwareReleaseManifestSchema.safeParse(manifestJson);
  if (!parsed.success) throw new Error('The firmware manifest does not match the locked PowerMeter release contract.');
  const manifest = parsed.data;
  if (image.size !== manifest.image_size) {
    throw new Error(`Firmware size mismatch: manifest requires ${manifest.image_size} bytes, selected binary has ${image.size}.`);
  }
  const actualSha256 = await sha256Hex(image);
  if (actualSha256 !== manifest.image_sha256) {
    throw new Error('Firmware SHA-256 does not match manifest.json. Select files from the same release.');
  }
  let releaseNotes = `Official PowerMeter firmware ${manifest.version}; source commit ${manifest.git_commit}; hardware certification ${manifest.hardware_certification}.`;
  if (releaseNotesFile) {
    if (releaseNotesFile.size > 65_536) throw new Error('Release notes exceed the 64 KiB input safety limit.');
    releaseNotes = (await releaseNotesFile.text()).trim();
    if (!releaseNotes) throw new Error('The selected release-notes file is empty.');
  }
  if (releaseNotes.length > 20_000) throw new Error('Release notes exceed the server limit of 20,000 characters.');
  return {
    fields: {
      semantic_version: manifest.version,
      build_number: manifest.build_number,
      board_profile: manifest.board_profile,
      minimum_boot_version: manifest.minimum_boot_version,
      minimum_config_version: manifest.minimum_config_version,
      expected_sha256: manifest.image_sha256,
      release_notes: releaseNotes,
    },
    imageSize: manifest.image_size,
    projectName: manifest.project_name,
    targetChip: manifest.target_chip,
    hardwareCertification: manifest.hardware_certification,
    gitCommit: manifest.git_commit,
  };
}
