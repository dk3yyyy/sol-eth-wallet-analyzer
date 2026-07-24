import { defineConfig } from '@playwright/test';

const python = process.env.PYTHON || 'python';

export default defineConfig({
  testDir: './tests-production',
  timeout: 30_000,
  expect: { timeout: 5_000 },
  use: {
    baseURL: 'http://127.0.0.1:4190',
    trace: 'retain-on-failure',
  },
  webServer: {
    command: `${python} -m uvicorn web_app:app --host 127.0.0.1 --port 4190`,
    url: 'http://127.0.0.1:4190/api/health',
    reuseExistingServer: false,
    timeout: 120_000,
  },
});
