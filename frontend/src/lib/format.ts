import { format, formatDistanceToNowStrict } from 'date-fns';

const numberFormat = new Intl.NumberFormat('en-US', { maximumFractionDigits: 2 });
const moneyFormat = new Intl.NumberFormat('en-US', { style: 'currency', currency: 'USD', minimumFractionDigits: 2, maximumFractionDigits: 2 });

function usableTimezone(value: string | null | undefined): value is string {
  if (!value) return false;
  try {
    new Intl.DateTimeFormat('en-US', { timeZone: value }).format(0);
    return true;
  } catch {
    return false;
  }
}

export function browserTimezone(): string | null {
  try {
    const timezone = Intl.DateTimeFormat().resolvedOptions().timeZone;
    return usableTimezone(timezone) ? timezone : null;
  } catch {
    return null;
  }
}

export function resolveDisplayTimezone(preferred?: string | null, homeTimezone?: string | null): string {
  if (usableTimezone(preferred)) return preferred;
  const browser = browserTimezone();
  if (browser) return browser;
  if (usableTimezone(homeTimezone)) return homeTimezone;
  return 'UTC';
}

export function numeric(value: number | null | undefined, unit = '', digits?: number): string {
  if (value === null || value === undefined) return 'Not available';
  let adjusted = value;
  let adjustedUnit = unit;
  const powerUnit = document.documentElement.dataset.powerUnit;
  const energyUnit = document.documentElement.dataset.energyUnit;
  if ((unit === 'kW' || unit === 'kilowatts') && powerUnit === 'W') {
    adjusted *= 1000;
    adjustedUnit = unit === 'kilowatts' ? 'watts' : 'W';
  }
  if (unit === 'kWh' && energyUnit === 'Wh') {
    adjusted *= 1000;
    adjustedUnit = 'Wh';
  }
  const configuredDigits = Number(document.documentElement.dataset.decimalPrecision ?? '2');
  const precision = digits ?? (Number.isInteger(configuredDigits) ? configuredDigits : 2);
  const text = new Intl.NumberFormat('en-US', { maximumFractionDigits: precision }).format(adjusted);
  return adjustedUnit ? `${text} ${adjustedUnit}` : text;
}

export function money(value: string | number | null | undefined): string {
  if (value === null || value === undefined) return 'Not available';
  return moneyFormat.format(Number(value));
}

export function percent(value: number | null | undefined, alreadyPercent = false): string {
  if (value === null || value === undefined) return 'Not available';
  return `${numberFormat.format(alreadyPercent ? value : value * 100)}%`;
}

export function timeAgo(value: string | null | undefined): string {
  if (!value) return 'Not available';
  return `${formatDistanceToNowStrict(new Date(value))} ago`;
}

export function dateTime(value: string | null | undefined, timezone?: string): string {
  if (!value) return 'Not available';
  const iso = document.documentElement.dataset.dateFormat === 'iso';
  const hour12 = document.documentElement.dataset.timeFormat !== '24h';
  const displayTimezone = resolveDisplayTimezone(timezone ?? document.documentElement.dataset.displayTimezone);
  return new Intl.DateTimeFormat(iso ? 'en-CA' : 'en-US', {
    year: 'numeric',
    month: iso ? '2-digit' : 'short',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    hour12,
    timeZone: displayTimezone,
    timeZoneName: 'short',
  }).format(new Date(value));
}

export function chartTick(epoch: number, rangeHours: number, timezone: string): string {
  const date = new Date(epoch);
  return new Intl.DateTimeFormat('en-US', rangeHours > 48
    ? { month: 'short', day: 'numeric', timeZone: timezone }
    : { hour: 'numeric', minute: '2-digit', timeZone: timezone }).format(date);
}

export interface ChartAxisFormat {
  width: number;
  tick: (value: number) => string;
}

export function chartAxisFormat(values: Array<number | null | undefined>, unit: string, minimumWidth = 58): ChartAxisFormat {
  const finite = values.filter((value): value is number => value !== null && value !== undefined && Number.isFinite(value));
  const maximum = finite.reduce((result, value) => Math.max(result, Math.abs(value)), 0);
  const digits = maximum > 0 && maximum < 1 ? 2 : maximum < 10 ? 1 : 0;
  const formatter = new Intl.NumberFormat('en-US', { minimumFractionDigits: digits === 1 ? 1 : 0, maximumFractionDigits: digits });
  const tick = (value: number) => unit === '$' ? `$${formatter.format(value)}` : `${formatter.format(value)}${unit ? ` ${unit}` : ''}`;
  const candidates = [tick(0), tick(maximum), tick(-maximum)];
  let measured = Math.max(...candidates.map((candidate) => candidate.length * 7));
  try {
    if (typeof navigator !== 'undefined' && navigator.userAgent.toLowerCase().includes('jsdom')) return { width: Math.max(minimumWidth, Math.ceil(measured) + 16), tick };
    const context = document.createElement('canvas').getContext('2d');
    if (context) {
      context.font = '11px system-ui, sans-serif';
      measured = Math.max(...candidates.map((candidate) => context.measureText(candidate).width));
    }
  } catch {
    // Character-width fallback above keeps server rendering and test DOMs deterministic.
  }
  return { width: Math.max(minimumWidth, Math.ceil(measured) + 16), tick };
}

export function inputDateTime(date: Date): string {
  return format(date, "yyyy-MM-dd'T'HH:mm");
}

export function bytes(value: number | null | undefined): string {
  if (value === null || value === undefined) return 'Not available';
  if (value < 1024) return `${value} B`;
  if (value < 1024 ** 2) return `${numberFormat.format(value / 1024)} KiB`;
  if (value < 1024 ** 3) return `${numberFormat.format(value / 1024 ** 2)} MiB`;
  return `${numberFormat.format(value / 1024 ** 3)} GiB`;
}

export function download(blob: Blob, filename: string): void {
  const url = URL.createObjectURL(blob);
  const link = document.createElement('a');
  link.href = url;
  link.download = filename;
  link.click();
  URL.revokeObjectURL(url);
}
