import { format, formatDistanceToNowStrict } from 'date-fns';

const numberFormat = new Intl.NumberFormat('en-US', { maximumFractionDigits: 2 });
const moneyFormat = new Intl.NumberFormat('en-US', { style: 'currency', currency: 'USD', minimumFractionDigits: 2, maximumFractionDigits: 2 });

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

export function dateTime(value: string | null | undefined, timezone = 'America/Los_Angeles'): string {
  if (!value) return 'Not available';
  const iso = document.documentElement.dataset.dateFormat === 'iso';
  const hour12 = document.documentElement.dataset.timeFormat !== '24h';
  return new Intl.DateTimeFormat(iso ? 'en-CA' : 'en-US', {
    year: 'numeric',
    month: iso ? '2-digit' : 'short',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    hour12,
    timeZone: timezone,
    timeZoneName: 'short',
  }).format(new Date(value));
}

export function chartTick(epoch: number, rangeHours: number, timezone: string): string {
  const date = new Date(epoch);
  return new Intl.DateTimeFormat('en-US', rangeHours > 48
    ? { month: 'short', day: 'numeric', timeZone: timezone }
    : { hour: 'numeric', minute: '2-digit', timeZone: timezone }).format(date);
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
