export function adaptiveTimeTicks(start: number, end: number, width: number): number[] {
  if (!Number.isFinite(start) || !Number.isFinite(end) || end <= start) return [];
  const minimumLabelWidth = end - start <= 6 * 3_600_000 ? 76 : end - start <= 48 * 3_600_000 ? 68 : 64;
  const maximumTicks = Math.max(2, Math.floor(Math.max(240, width - 70) / minimumLabelWidth));
  const count = Math.min(maximumTicks, 8);
  if (count === 2) return [start, end];
  const step = (end - start) / (count - 1);
  return Array.from({ length: count }, (_, index) => index === count - 1 ? end : Math.round(start + step * index));
}

export interface DailyEnergyInputPoint {
  timestamp: string;
  epoch: number;
  value: string | number | null;
  cost: string | number | null;
  quality: string | number | null;
}

export interface DailyEnergyConnectionGap {
  event_id?: string | undefined;
  start_utc: string;
  end_utc: string;
  recovered_energy_kwh: string | number | null;
  status: 'recovered' | 'unresolved';
}

export interface DailyEnergyEstimateDetail {
  event_id?: unknown;
  status?: unknown;
  energy_kwh?: unknown;
}

export interface DailyEnergyDay {
  timestamp: string;
  epoch: number;
  value: number | null;
  cost: number | null;
  quality: number | null;
  acceptedEnergyKwh: number | null;
  recoveredEnergyKwh: number;
  estimatedEnergyKwh: number;
  hasMissingIntervals: boolean;
  source: 'bounded-intervals' | 'calendar-summaries';
}

export interface UnallocatedGapEnergy {
  eventId: string | null;
  energyKwh: number | null;
  kind: 'recovered' | 'estimated' | 'unknown';
  reason: 'crosses_local_day' | 'partially_selected' | 'unsupported_gap_evidence';
}

export function localCalendarDay(epoch: number, timezone: string): string {
  const parts = new Intl.DateTimeFormat('en-CA', { year: 'numeric', month: '2-digit', day: '2-digit', timeZone: timezone }).formatToParts(new Date(epoch));
  const value = (type: Intl.DateTimeFormatPartTypes) => parts.find((part) => part.type === type)?.value ?? '';
  return `${value('year')}-${value('month')}-${value('day')}`;
}

function finiteNumber(value: unknown): number | null {
  if (value === null || value === undefined || value === '') return null;
  const number = Number(value);
  return Number.isFinite(number) ? number : null;
}

export function groupDailyEnergy(params: {
  points: DailyEnergyInputPoint[];
  gaps: DailyEnergyConnectionGap[];
  estimates: DailyEnergyEstimateDetail[];
  rangeStart: number;
  rangeEnd: number;
  timezone: string;
}): { days: DailyEnergyDay[]; unallocated: UnallocatedGapEnergy[]; assignedTotalKwh: number | null } {
  const { points, gaps, estimates, rangeStart, rangeEnd, timezone } = params;
  const grouped = new Map<string, DailyEnergyDay>();
  const estimateByEvent = new Map<string, number>();
  for (const detail of estimates) {
    if (detail.status !== 'estimated' || typeof detail.event_id !== 'string') continue;
    const energy = finiteNumber(detail.energy_kwh);
    if (energy !== null) estimateByEvent.set(detail.event_id, energy);
  }
  const ensureDay = (day: string, epoch: number, timestamp: string) => {
    const current = grouped.get(day);
    if (current) return current;
    const created: DailyEnergyDay = {
      timestamp, epoch, value: null, cost: null, quality: null, acceptedEnergyKwh: null,
      recoveredEnergyKwh: 0, estimatedEnergyKwh: 0, hasMissingIntervals: false, source: 'bounded-intervals',
    };
    grouped.set(day, created);
    return created;
  };
  for (const point of points) {
    if (point.epoch < rangeStart || point.epoch >= rangeEnd) continue;
    const day = localCalendarDay(point.epoch, timezone);
    const current = ensureDay(day, point.epoch, point.timestamp);
    const value = finiteNumber(point.value);
    const cost = finiteNumber(point.cost);
    const quality = finiteNumber(point.quality);
    current.acceptedEnergyKwh = value === null ? current.acceptedEnergyKwh : (current.acceptedEnergyKwh ?? 0) + value;
    current.cost = cost === null ? current.cost : (current.cost ?? 0) + cost;
    current.quality = quality === null ? current.quality : Math.min(current.quality ?? quality, quality);
    current.hasMissingIntervals ||= value === null;
  }
  const unallocated: UnallocatedGapEnergy[] = [];
  for (const gap of gaps) {
    const start = new Date(gap.start_utc).getTime();
    const end = new Date(gap.end_utc).getTime();
    if (!Number.isFinite(start) || !Number.isFinite(end) || end <= start || end <= rangeStart || start >= rangeEnd) continue;
    const recovered = gap.status === 'recovered' ? finiteNumber(gap.recovered_energy_kwh) : null;
    const estimated = gap.event_id ? estimateByEvent.get(gap.event_id) ?? null : null;
    const kind: UnallocatedGapEnergy['kind'] = recovered !== null ? 'recovered' : estimated !== null ? 'estimated' : 'unknown';
    const energyKwh = recovered ?? estimated;
    const startDay = localCalendarDay(start, timezone);
    const endDay = localCalendarDay(end - 1, timezone);
    const fullySelected = start >= rangeStart && end <= rangeEnd;
    if (!fullySelected || startDay !== endDay || energyKwh === null) {
      unallocated.push({
        eventId: gap.event_id ?? null,
        energyKwh,
        kind,
        reason: !fullySelected ? 'partially_selected' : startDay !== endDay ? 'crosses_local_day' : 'unsupported_gap_evidence',
      });
      continue;
    }
    const current = ensureDay(startDay, start, gap.start_utc);
    if (kind === 'recovered') current.recoveredEnergyKwh += energyKwh;
    if (kind === 'estimated') current.estimatedEnergyKwh += energyKwh;
  }
  const days = [...grouped.values()].sort((left, right) => left.epoch - right.epoch).map((day) => {
    const hasAssignedEnergy = day.acceptedEnergyKwh !== null || day.recoveredEnergyKwh !== 0 || day.estimatedEnergyKwh !== 0;
    return { ...day, value: hasAssignedEnergy ? (day.acceptedEnergyKwh ?? 0) + day.recoveredEnergyKwh + day.estimatedEnergyKwh : null };
  });
  const assignedValues = days.map((day) => day.value).filter((value): value is number => value !== null);
  return {
    days,
    unallocated,
    assignedTotalKwh: assignedValues.length > 0 ? assignedValues.reduce((total, value) => total + value, 0) : null,
  };
}
