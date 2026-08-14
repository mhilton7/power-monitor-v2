import { expect, test } from '@playwright/test';
import { apiResponse, session } from '../fixtures';

test('login reaches the authenticated Home dashboard', async ({ page }) => {
  let authenticated = false;
  await page.route('**/api/v1/**', async (route) => {
    const path = new URL(route.request().url()).pathname;
    if (path.endsWith('/auth/bootstrap/status')) {
      await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ required: false }) });
    } else if (path.endsWith('/auth/me')) {
      await route.fulfill({ status: authenticated ? 200 : 401, contentType: 'application/json', body: JSON.stringify(authenticated ? session.user : { title: 'Unauthorized', status: 401 }) });
    } else if (path.endsWith('/auth/login')) {
      authenticated = true;
      await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ user: session.user }) });
    } else {
      await route.fulfill({ status: 404, contentType: 'application/json', body: '{}' });
    }
  });
  await page.goto('/');
  await page.getByLabel('Email').fill('alex@example.test');
  await page.getByLabel('Password').fill('a-strong-server-password');
  await page.getByRole('button', { name: 'Sign in' }).click();
  await expect(page.getByRole('navigation', { name: 'Primary navigation' }).first().getByText('Home')).toBeVisible();
});

test('first-run setup creates the protected owner', async ({ page }) => {
  let bootstrapped = false;
  await page.route('**/api/v1/**', async (route) => {
    const path = new URL(route.request().url()).pathname;
    if (path.endsWith('/auth/bootstrap/status')) {
      await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ required: !bootstrapped }) });
    } else if (path.endsWith('/auth/bootstrap')) {
      bootstrapped = true;
      await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ user: session.user }) });
    } else if (path.endsWith('/auth/me')) {
      await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(session.user) });
    } else {
      const response = apiResponse(path, route.request().method());
      await route.fulfill({ status: response.status, contentType: response.contentType ?? 'application/json', body: JSON.stringify(response.body) });
    }
  });
  await page.goto('/');
  await expect(page.getByRole('heading', { name: 'Create the owner account' })).toBeVisible();
  await page.getByLabel('Display name').fill('Alex Morgan');
  await page.getByLabel('Email').fill('alex@example.test');
  await page.getByLabel('Password').fill('a-strong-server-password');
  await page.getByRole('button', { name: 'Create owner' }).click();
  await expect(page.getByText('Live Power Usage')).toBeVisible();
});
