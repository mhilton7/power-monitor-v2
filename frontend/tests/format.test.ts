import { browserTimezone, chartAxisFormat, dateTime, dateTimeRange, resolveDisplayTimezone } from '../src/lib/format';

describe('display formatting', () => {
  it('honors an explicit timezone before browser and home fallbacks', () => {
    expect(resolveDisplayTimezone('America/New_York', 'America/Los_Angeles')).toBe('America/New_York');
    expect(resolveDisplayTimezone(undefined, 'America/Los_Angeles')).toBe(browserTimezone() ?? 'America/Los_Angeles');
    expect(resolveDisplayTimezone('not/a-timezone', 'America/Los_Angeles')).toBe(browserTimezone() ?? 'America/Los_Angeles');
  });

  it('formats Pacific winter and summer instants with the correct DST abbreviation', () => {
    expect(dateTime('2026-01-15T20:00:00Z', 'America/Los_Angeles')).toContain('PST');
    expect(dateTime('2026-08-15T20:00:00Z', 'America/Los_Angeles')).toContain('PDT');
  });

  it('formats a compact selected chart range without repeating full timestamps', () => {
    const range = dateTimeRange('2026-08-13T10:00:00Z', '2026-08-13T22:00:00Z', 'America/Los_Angeles');
    expect(range).toContain('PDT');
    expect(range).toContain('Aug 13');
    expect(range).not.toContain('2026');
  });

  it('measures enough y-axis space for readable power and energy units', () => {
    const power = chartAxisFormat([0, 0.5, 2], 'kW');
    const energy = chartAxisFormat([0, 951], 'kWh');
    expect(power.tick(0.5)).toBe('0.5 kW');
    expect(energy.tick(951)).toBe('951 kWh');
    expect(power.width).toBeGreaterThanOrEqual(58);
    expect(energy.width).toBeGreaterThan(power.width);
  });
});
