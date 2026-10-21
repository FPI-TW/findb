import { playwright } from "@vitest/browser-playwright"
import tailwindcss from "@tailwindcss/vite"
import { defineConfig } from "vitest/config"

export default defineConfig({
  plugins: [tailwindcss()],
  optimizeDeps: {
    exclude: ["@tanstack/react-start", "@tanstack/react-start/server"],
  },
  test: {
    include: ["src/**/*.browser.test.tsx"],
    browser: {
      enabled: true,
      headless: true,
      // Pin zone and locale so formatting is identical on every host and in CI.
      provider: playwright({
        contextOptions: { timezoneId: "UTC", locale: "zh-TW" },
      }),
      screenshotFailures: false,
      instances: [{ browser: "chromium" }],
      viewport: { width: 1440, height: 900 },
    },
  },
})
