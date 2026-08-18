import { playwright } from "@vitest/browser-playwright"
import tailwindcss from "@tailwindcss/vite"
import { defineConfig } from "vitest/config"

export default defineConfig({
  plugins: [tailwindcss()],
  test: {
    include: ["src/**/*.browser.test.tsx"],
    browser: {
      enabled: true,
      headless: true,
      provider: playwright(),
      screenshotFailures: false,
      instances: [{ browser: "chromium" }],
      viewport: { width: 1440, height: 900 },
    },
  },
})
