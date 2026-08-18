import { defineConfig } from "vitest/config"

export default defineConfig({
  test: {
    environment: "jsdom",
    include: ["src/**/*.test.{ts,tsx}"],
    exclude: ["src/**/*.browser.test.tsx"],
    setupFiles: ["./vitest.setup.js"],
  },
})
