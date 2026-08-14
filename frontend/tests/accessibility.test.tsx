import { screen } from '@testing-library/react';
import { axe, toHaveNoViolations } from 'jest-axe';
import { HomePage } from '../src/pages/HomePage';
import { installFetchMock, renderWithProviders } from './render';

expect.extend(toHaveNoViolations);

describe('accessibility', () => {
  it('has no detectable high-confidence accessibility violations on Home', async () => {
    installFetchMock();
    const { container } = renderWithProviders(<HomePage />);
    expect(await screen.findByRole('heading', { name: 'Live Power Usage' })).toBeInTheDocument();
    expect(await axe(container, { rules: { region: { enabled: false } } })).toHaveNoViolations();
  });
});
