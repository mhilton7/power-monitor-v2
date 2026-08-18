import { screen } from '@testing-library/react';
import { BillingPage } from '../src/pages/BillingPage';
import { HistoryPage } from '../src/pages/HistoryPage';
import { installFetchMock, renderWithProviders } from './render';

const prohibitedNormalCopy = [
  'verified aggregate',
  'committed sensor evidence',
  'missing-data evidence',
  'immutable published assignment',
  'allowlisted synchronous validation',
  'active provenance',
];

function expectPlainLanguage(): void {
  const output = document.body.textContent?.toLowerCase() ?? '';
  for (const phrase of prohibitedNormalCopy) expect(output).not.toContain(phrase);
}

describe('Normal user-facing language', () => {
  it('uses saved-reading and service-branch terms in History', async () => {
    installFetchMock();
    renderWithProviders(<HistoryPage />);
    expect(await screen.findByLabelText('Service branch or sensor')).toBeInTheDocument();
    expect(screen.getByText('Saved sensor readings')).toBeInTheDocument();
    expectPlainLanguage();
  });

  it('uses understandable rate and cycle section names in Billing', async () => {
    installFetchMock();
    renderWithProviders(<BillingPage />);
    expect(await screen.findByRole('heading', { name: 'Rate currently used' })).toBeInTheDocument();
    expect(screen.getByRole('heading', { name: 'Current billing cycle' })).toBeInTheDocument();
    expect(screen.getByRole('heading', { name: 'SCE rate update' })).toBeInTheDocument();
    expectPlainLanguage();
  });
});
