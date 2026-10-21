import { defineConfig } from "vitest/config"

// Pin the zone so local-time formatting is identical on every host and in CI.
process.env.TZ = "UTC"

export default defineConfig({
  test: {
    environment: "jsdom",
    env: { TZ: "UTC" },
    include: ["src/**/*.test.{ts,tsx}"],
    exclude: ["src/**/*.browser.test.tsx"],
    setupFiles: ["./vitest.setup.js"],
  },
})
