import { expect, test } from '@playwright/test';

const expectedCsp = [
  "default-src 'self'",
  "connect-src 'self'",
  "frame-ancestors 'none'",
  "script-src 'self'",
  "style-src 'self'",
];

test('serves the production build with working assets and security headers', async ({ page }) => {
  const consoleErrors = [];
  const failedRequests = [];
  page.on('console', (message) => {
    if (message.type() === 'error') consoleErrors.push(message.text());
  });
  page.on('requestfailed', (request) => failedRequests.push(request.url()));

  const response = await page.goto('/');
  expect(response?.status()).toBe(200);
  expect(response?.headers()['cache-control']).toBe('no-store');
  expect(response?.headers()['x-content-type-options']).toBe('nosniff');
  expect(response?.headers()['x-frame-options']).toBe('DENY');
  expect(response?.headers()['referrer-policy']).toBe('no-referrer');

  const csp = response?.headers()['content-security-policy'] || '';
  for (const directive of expectedCsp) expect(csp).toContain(directive);

  await expect(page.getByRole('heading', { name: 'A clear view of any wallet.' })).toBeVisible();
  expect(consoleErrors).toEqual([]);
  expect(failedRequests).toEqual([]);
});

test('exposes health and rejects invalid analysis without provider access', async ({ request }) => {
  const health = await request.get('/api/health');
  expect(health.status()).toBe(200);
  await expect(health.json()).resolves.toEqual({
    status: 'ok',
    supported_chains: ['solana', 'ethereum'],
  });

  const invalid = await request.post('/api/analyze', {
    data: { address: 'not-a-wallet' },
  });
  expect(invalid.status()).toBe(422);
  await expect(invalid.json()).resolves.toEqual({
    detail: 'Enter a valid Solana or Ethereum wallet address.',
  });
});
