import { expect, test } from '@playwright/test';

const ethereumAddress = `0x${'1'.repeat(40)}`;
const sampleResult = {
  address: ethereumAddress,
  chain: 'ethereum',
  native_asset: {
    symbol: 'ETH',
    balance: 1,
    price_usd: 2500,
    value_usd: 2500,
  },
  total_value_usd: 2500,
  tokens: [],
  token_summary: {
    holding_count: 0,
    valued_count: 0,
    unavailable_count: 0,
    value_usd: 0,
    allocation_percent: 0,
  },
  warnings: [],
  explorer_url: `https://etherscan.io/address/${ethereumAddress}`,
  updated_at: '2026-07-24T12:00:00Z',
};

test('analyzes a wallet and renders an accessible portfolio summary', async ({ page }) => {
  await page.route('**/api/analyze', async (route) => {
    expect(route.request().postDataJSON()).toEqual({ address: ethereumAddress });
    await route.fulfill({ json: sampleResult });
  });

  await page.goto('/');
  await expect(page.getByRole('heading', { name: 'A clear view of any wallet.' })).toBeVisible();
  await page.getByLabel('Wallet address').fill(ethereumAddress);
  await page.getByRole('button', { name: 'Analyze wallet' }).click();

  await expect(page.getByRole('heading', { name: '$2,500.00' })).toBeVisible();
  await expect(page.getByText('Ethereum', { exact: true })).toBeVisible();
  await expect(page.getByText('1 ETH')).toBeVisible();
  const explorer = page.getByRole('link', { name: 'View on Etherscan' });
  await expect(explorer).toHaveAttribute('href', sampleResult.explorer_url);
  await expect(page.getByRole('status')).toContainText('Ethereum wallet analysis complete');
});

test('announces partial market coverage when analysis completes', async ({ page }) => {
  await page.route('**/api/analyze', (route) =>
    route.fulfill({
      json: {
        ...sampleResult,
        warnings: ['Market data was unavailable for 1 holding.'],
      },
    }),
  );

  await page.goto('/');
  await page.getByLabel('Wallet address').fill(ethereumAddress);
  await page.getByRole('button', { name: 'Analyze wallet' }).click();

  await expect(page.getByRole('status')).toContainText(
    'Partial market coverage: Market data was unavailable for 1 holding.',
  );
});

test('shows actionable errors and keeps the submitted address available', async ({ page }) => {
  await page.route('**/api/analyze', (route) =>
    route.fulfill({
      status: 503,
      contentType: 'application/json',
      body: JSON.stringify({
        detail: 'Wallet data providers are temporarily unavailable. Try again shortly.',
      }),
    }),
  );

  await page.goto('/');
  await page.getByLabel('Wallet address').fill(ethereumAddress);
  await page.getByRole('button', { name: 'Analyze wallet' }).click();

  await expect(page.getByRole('alert')).toContainText('temporarily unavailable');
  await expect(page.getByLabel('Wallet address')).toHaveValue(ethereumAddress);
});

test('uses no third-party runtime requests or browser storage', async ({ page }) => {
  const externalRequests = [];
  page.on('request', (request) => {
    const url = new URL(request.url());
    if (url.hostname !== '127.0.0.1') externalRequests.push(request.url());
  });

  await page.goto('/');
  expect(externalRequests).toEqual([]);
  expect(await page.evaluate(() => localStorage.length)).toBe(0);
  expect(await page.evaluate(() => sessionStorage.length)).toBe(0);
});

test('has no horizontal overflow at a mobile viewport', async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto('/');

  const widths = await page.evaluate(() => ({
    scroll: document.documentElement.scrollWidth,
    client: document.documentElement.clientWidth,
  }));
  expect(widths.scroll).toBeLessThanOrEqual(widths.client);
  await expect(page.getByRole('button', { name: 'Analyze wallet' })).toBeVisible();
});
