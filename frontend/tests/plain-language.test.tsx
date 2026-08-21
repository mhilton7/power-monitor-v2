import { screen } from '@testing-library/react';
import { BillingPage } from '../src/pages/BillingPage';
import { HistoryPage } from '../src/pages/HistoryPage';
import { SettingsPage } from '../src/pages/SettingsPage';
import { installFetchMock, renderWithProviders } from './render';

const prohibitedNormalCopy = [
  'verified aggregate',
  'committed sensor evidence',
  'missing-data evidence',
  'immutable published assignment',
  'allowlisted synchronous validation',
  'active provenance',
  'microsd',
  'backlog',
  'waiting to sync',
  'server acknowledged',
];

function expectPlainLanguage(): void {
  const output = document.body.textContent?.toLowerCase() ?? '';
  for (const phrase of prohibitedNormalCopy) expect(output).not.toContain(phrase);
}

describe('Normal user-facing language', () => {
  it('uses accepted-reading and service-branch terms in History', async () => {
    installFetchMock();
    renderWithProviders(<HistoryPage />);
    expect(await screen.findByLabelText('Service branch or sensor')).toBeInTheDocument();
    expect(screen.getByText('Accepted sensor readings')).toBeInTheDocument();
    expectPlainLanguage();
  });

  it('uses understandable rate and cycle section names in Billing', async () => {
    installFetchMock();
    renderWithProviders(<BillingPage />);
    expect(await screen.findByRole('heading', { name: 'Current Rate Plan' })).toBeInTheDocument();
    expect(screen.getByRole('heading', { name: 'Current Billing Cycle' })).toBeInTheDocument();
    expect(screen.getByRole('heading', { name: 'Tier Breakdown' })).toBeInTheDocument();
    expect(screen.getByRole('heading', { name: 'Cost Summary' })).toBeInTheDocument();
    expect(screen.getByRole('link', { name: 'Manage billing settings' })).toHaveAttribute('href', '/settings?section=rates');
    expectPlainLanguage();
  });

  it('keeps stateless sensor storage and queue terms out of normal Settings', async () => {
    window.history.replaceState(null, '', '/settings?section=firmware');
    installFetchMock();
    renderWithProviders(<SettingsPage />);
    expect(await screen.findByRole('heading', { name: 'Firmware' })).toBeInTheDocument();
    expectPlainLanguage();
  });
});
