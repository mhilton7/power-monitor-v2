import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { useState } from 'react';
import { Dialog } from '../src/components/ui';

function NestedDialogs() {
  const [outerOpen, setOuterOpen] = useState(false);
  const [innerOpen, setInnerOpen] = useState(false);
  return <>
    <button type="button" onClick={() => setOuterOpen(true)}>Open outer</button>
    <Dialog open={outerOpen} title="Outer dialog" onClose={() => setOuterOpen(false)}>
      <button type="button" onClick={() => setInnerOpen(true)}>Open inner</button>
      <button type="button">Outer last action</button>
      <Dialog open={innerOpen} title="Inner dialog" onClose={() => setInnerOpen(false)}>
        <button type="button">Inner action</button>
      </Dialog>
    </Dialog>
  </>;
}

describe('Dialog', () => {
  it('traps focus, keeps only the top layer active, and restores focus and page state', async () => {
    const root = document.createElement('div');
    root.id = 'root';
    document.body.append(root);
    const view = render(<NestedDialogs />, { container: root });
    const opener = screen.getByRole('button', { name: 'Open outer' });
    await userEvent.click(opener);

    const outer = screen.getByRole('dialog', { name: 'Outer dialog' });
    expect(root).toHaveAttribute('inert');
    expect(root).toHaveAttribute('aria-hidden', 'true');
    expect(document.body).toHaveStyle({ overflow: 'hidden' });
    expect(screen.getByRole('button', { name: 'Close Outer dialog' })).toHaveFocus();
    await userEvent.tab({ shift: true });
    expect(screen.getByRole('button', { name: 'Outer last action' })).toHaveFocus();
    await userEvent.tab();
    expect(screen.getByRole('button', { name: 'Close Outer dialog' })).toHaveFocus();

    const innerOpener = screen.getByRole('button', { name: 'Open inner' });
    await userEvent.click(innerOpener);
    expect(screen.getByRole('dialog', { name: 'Inner dialog' })).toBeInTheDocument();
    const outerLayer = outer.closest('.dialog-backdrop');
    expect(outerLayer).toHaveAttribute('inert');
    expect(outerLayer).toHaveAttribute('aria-hidden', 'true');
    expect(screen.getByRole('button', { name: 'Close Inner dialog' })).toHaveFocus();

    await userEvent.keyboard('{Escape}');
    await waitFor(() => expect(screen.queryByRole('dialog', { name: 'Inner dialog' })).not.toBeInTheDocument());
    expect(outerLayer).not.toHaveAttribute('inert');
    expect(innerOpener).toHaveFocus();
    expect(document.body).toHaveStyle({ overflow: 'hidden' });

    await userEvent.keyboard('{Escape}');
    await waitFor(() => expect(screen.queryByRole('dialog', { name: 'Outer dialog' })).not.toBeInTheDocument());
    expect(root).not.toHaveAttribute('inert');
    expect(root).not.toHaveAttribute('aria-hidden');
    expect(document.body.style.overflow).toBe('');
    expect(opener).toHaveFocus();
    view.unmount();
    root.remove();
  });
});
