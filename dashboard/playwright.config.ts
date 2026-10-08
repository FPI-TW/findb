import { defineConfig } from "@playwright/test"

export default defineConfig({
  testDir: "./e2e",
  outputDir: ".playwright-results",
  fullyParallel: false,
  retries: 0,
  reporter: "line",
  use: {
    baseURL: "http://127.0.0.1:3000",
    browserName: "chromium",
    trace: "retain-on-failure",
  },
  webServer: [
    {
      command: "node e2e/fixtures/operations-backend.ts",
      url: "http://127.0.0.1:18081/health",
      reuseExistingServer: false,
    },
    {
      command: "pnpm dev --host 127.0.0.1",
      url: "http://127.0.0.1:3000/dashboard/",
      reuseExistingServer: false,
      timeout: 30_000,
      env: { FINDB_API_BASE_URL: "http://127.0.0.1:18081" },
    },
  ],
})
