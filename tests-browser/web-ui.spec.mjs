import { readFile } from 'node:fs/promises';
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

const holdingsResult = {
  ...sampleResult,
  chain: 'solana',
  native_asset: { symbol: 'SOL', balance: 10, price_usd: 150, value_usd: 1500 },
  total_value_usd: 1600.5,
  token_summary: {
    holding_count: 3,
    valued_count: 2,
    unavailable_count: 1,
    value_usd: 100.5,
    allocation_percent: 6.3,
  },
  tokens: [
    { mint: 'mint-beta', symbol: 'BETA', name: 'Beta Token', balance: 20, price_usd: 5, value_usd: 100, price_change_24h_percent: 2, liquidity_usd: 100000, market_url: null, logo_available: true },
    { mint: 'mint-alpha', symbol: 'ALPHA', name: 'Alpha Token', balance: 5, price_usd: 0.1, value_usd: 0.5, price_change_24h_percent: -1, liquidity_usd: 5000, market_url: null },
    { mint: 'mint-unknown', symbol: 'UNKNOWN', name: 'Unpriced Token', balance: 50, price_usd: null, value_usd: null, price_change_24h_percent: null, liquidity_usd: null, market_url: null },
  ],
  explorer_url: 'https://solscan.io/account/11111111111111111111111111111111',
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

test('renders same-origin token logos and keeps monograms as the fallback', async ({ page }) => {
  const externalRequests = [];
  page.on('request', (request) => {
    const url = new URL(request.url());
    if (url.hostname !== '127.0.0.1') externalRequests.push(request.url());
  });
  await page.route('**/api/analyze', (route) => route.fulfill({ json: holdingsResult }));
  await page.route('**/api/token-logo/mint-beta', (route) => route.fulfill({
    contentType: 'image/png',
    body: Buffer.from('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII=', 'base64'),
  }));

  await page.goto('/');
  await page.getByLabel('Wallet address').fill(ethereumAddress);
  await page.getByRole('button', { name: 'Analyze wallet' }).click();

  const betaRow = page.locator('tbody tr').filter({ hasText: 'BETA' });
  await expect(betaRow.locator('img.token-logo')).toBeVisible();
  await expect(betaRow.locator('.asset-monogram')).toHaveCount(0);
  const alphaRow = page.locator('tbody tr').filter({ hasText: 'ALPHA' });
  await expect(alphaRow.locator('.asset-monogram')).toBeVisible();
  expect(externalRequests).toEqual([]);
});

test('filters, sorts, and hides only valued dust holdings', async ({ page }) => {
  await page.route('**/api/analyze', (route) => route.fulfill({ json: holdingsResult }));
  await page.goto('/');
  await page.getByLabel('Wallet address').fill(ethereumAddress);
  await page.getByRole('button', { name: 'Analyze wallet' }).click();

  const dataRows = page.locator('tbody tr');
  await expect(dataRows).toHaveCount(3);
  await expect(dataRows.nth(0)).toContainText('BETA');

  await page.getByLabel('Sort holdings').selectOption('symbol');
  await expect(dataRows.nth(0)).toContainText('ALPHA');

  await page.getByLabel('Search token holdings').fill('beta');
  await expect(dataRows).toHaveCount(1);
  await expect(dataRows.nth(0)).toContainText('BETA');

  await page.getByLabel('Search token holdings').fill('');
  await page.getByLabel('Hide holdings under $1').check();
  await expect(dataRows).toHaveCount(2);
  await expect(page.getByText('ALPHA', { exact: true })).toHaveCount(0);
  await expect(page.getByText('UNKNOWN', { exact: true })).toBeVisible();
  await expect(page.getByText('Showing 2 of 3 holdings')).toBeVisible();
});

test('exports the current snapshot as browser-generated CSV and JSON files', async ({ page }) => {
  await page.route('**/api/analyze', (route) => route.fulfill({ json: holdingsResult }));
  await page.goto('/');
  await page.getByLabel('Wallet address').fill(ethereumAddress);
  await page.getByRole('button', { name: 'Analyze wallet' }).click();

  const csvDownloadPromise = page.waitForEvent('download');
  await page.getByRole('button', { name: 'Export CSV' }).click();
  const csvDownload = await csvDownloadPromise;
  expect(csvDownload.suggestedFilename()).toBe('chainscope-holdings.csv');
  const csvPath = await csvDownload.path();
  expect(await readFile(csvPath, 'utf8')).toContain('Native asset,SOL');
  expect(await readFile(csvPath, 'utf8')).toContain('Beta Token,BETA');

  const jsonDownloadPromise = page.waitForEvent('download');
  await page.getByRole('button', { name: 'Export JSON' }).click();
  const jsonDownload = await jsonDownloadPromise;
  expect(jsonDownload.suggestedFilename()).toBe('chainscope-snapshot.json');
  const jsonPath = await jsonDownload.path();
  expect(JSON.parse(await readFile(jsonPath, 'utf8')).address).toBe(ethereumAddress);
});

test('copies a fragment-only share link and pre-fills it without automatic analysis', async ({ page, context }) => {
  await context.grantPermissions(['clipboard-read', 'clipboard-write']);
  let analyzeRequests = 0;
  await page.route('**/api/analyze', async (route) => {
    analyzeRequests += 1;
    await route.fulfill({ json: sampleResult });
  });

  await page.goto('/');
  await page.getByLabel('Wallet address').fill(ethereumAddress);
  await page.getByRole('button', { name: 'Analyze wallet' }).click();
  await page.getByRole('button', { name: 'Copy share link' }).click();

  const copied = await page.evaluate(() => navigator.clipboard.readText());
  expect(copied).toContain(`#address=${ethereumAddress}`);
  expect(copied).not.toContain('?address=');
  await expect(page.getByRole('status')).toContainText('Share link copied');

  await page.goto(`/#address=${ethereumAddress}`);
  await expect(page.getByLabel('Wallet address')).toHaveValue(ethereumAddress);
  expect(analyzeRequests).toBe(1);
  expect(await page.evaluate(() => localStorage.length + sessionStorage.length)).toBe(0);
});

test('shows visible loading feedback and retries a failed analysis', async ({ page }) => {
  let attempt = 0;
  await page.route('**/api/analyze', async (route) => {
    attempt += 1;
    if (attempt === 1) {
      await route.fulfill({
        status: 503,
        contentType: 'application/json',
        body: JSON.stringify({ detail: 'Wallet data providers are temporarily unavailable. Try again shortly.' }),
      });
      return;
    }
    await route.fulfill({ json: sampleResult });
  });

  await page.goto('/');
  await page.getByLabel('Wallet address').fill(ethereumAddress);
  await page.getByRole('button', { name: 'Analyze wallet' }).click();
  await expect(page.getByRole('alert')).toContainText('temporarily unavailable');
  await page.getByRole('button', { name: 'Retry analysis' }).click();
  await expect(page.getByRole('heading', { name: '$2,500.00' })).toBeVisible();
  expect(attempt).toBe(2);

  let releaseResponse;
  await page.route('**/api/analyze', async (route) => {
    await new Promise((resolve) => { releaseResponse = resolve; });
    await route.fulfill({ json: sampleResult });
  });
  await page.getByRole('button', { name: 'Analyze wallet' }).click();
  await expect(page.getByLabel('Analysis in progress')).toBeVisible();
  releaseResponse();
  await expect(page.getByLabel('Analysis in progress')).toHaveCount(0);
});

test('refreshes a completed analysis without discarding the previous result on failure', async ({ page }) => {
  const requests = [];
  await page.route('**/api/analyze', async (route) => {
    requests.push(route.request().postDataJSON());
    if (requests.length === 1) {
      await route.fulfill({ json: sampleResult });
      return;
    }
    await route.fulfill({
      status: 503,
      contentType: 'application/json',
      body: JSON.stringify({ detail: 'Wallet data providers are temporarily unavailable. Try again shortly.' }),
    });
  });

  await page.goto('/');
  await page.getByLabel('Wallet address').fill(ethereumAddress);
  await page.getByRole('button', { name: 'Analyze wallet' }).click();
  await expect(page.getByRole('heading', { name: '$2,500.00' })).toBeVisible();

  await page.getByRole('button', { name: 'Refresh data' }).click();

  expect(requests).toEqual([
    { address: ethereumAddress },
    { address: ethereumAddress, force_refresh: true },
  ]);
  await expect(page.getByRole('heading', { name: '$2,500.00' })).toBeVisible();
  await expect(page.getByRole('alert')).toContainText('temporarily unavailable');
  await expect(page.getByRole('status')).toContainText('Refresh failed. Previous snapshot retained.');
});

test('has no horizontal overflow with result controls at a mobile viewport', async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await page.route('**/api/analyze', (route) => route.fulfill({ json: holdingsResult }));
  await page.goto('/');
  await page.getByLabel('Wallet address').fill(ethereumAddress);
  await page.getByRole('button', { name: 'Analyze wallet' }).click();
  await expect(page.getByLabel('Search token holdings')).toBeVisible();
  await expect(page.getByRole('button', { name: 'Export CSV' })).toBeVisible();
  const widths = await page.evaluate(() => ({
    scroll: document.documentElement.scrollWidth,
    client: document.documentElement.clientWidth,
  }));
  expect(widths.scroll).toBe(widths.client);
});
