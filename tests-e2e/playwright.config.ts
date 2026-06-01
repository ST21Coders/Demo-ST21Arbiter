import { defineConfig, devices } from '@playwright/test';

const TEST_MODE = process.env.TEST_MODE ?? 'mock';
const BASE_URL = process.env.BASE_URL ?? 'http://localhost:5173';

export default defineConfig({
  testDir: './tests',
  fullyParallel: true,
  forbidOnly: !!process.env.CI,
  retries: process.env.CI ? 1 : 0,
  workers: process.env.CI ? 2 : undefined,
  reporter: [
    ['list'],
    ['json', { outputFile: '../test-reports/playwright-results.json' }],
    ['html', { outputFolder: '../test-reports/playwright-html', open: 'never' }],
  ],
  use: {
    baseURL: BASE_URL,
    trace: 'retain-on-failure',
    screenshot: 'only-on-failure',
    video: 'retain-on-failure',
  },
  projects: [
    {
      name: 'chromium',
      use: { ...devices['Desktop Chrome'] },
    },
  ],
  webServer:
    TEST_MODE === 'mock'
      ? {
          // Mock mode: start the local Vite dev server with VITE_API_URL unset so
          // the UI's USE_MOCK switch flips on (see ui/src/config.js:14).
          command: 'cd ../ui && VITE_API_URL= VITE_CHAT_URL= npm run dev -- --port 5173',
          url: 'http://localhost:5173',
          reuseExistingServer: !process.env.CI,
          timeout: 60_000,
        }
      : undefined,
});
