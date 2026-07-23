import { defineConfig } from "vite"
import { devtools } from "@tanstack/devtools-vite"

import { tanstackStart } from "@tanstack/react-start/plugin/vite"

import viteReact from "@vitejs/plugin-react"
import tailwindcss from "@tailwindcss/vite"
import { nitro } from "nitro/vite"

import { DASHBOARD_BASE_PATH, DASHBOARD_BASE_URL } from "./src/lib/paths"

const config = defineConfig({
  base: DASHBOARD_BASE_URL,
  resolve: { tsconfigPaths: true },
  plugins: [
    devtools(),
    nitro({
      baseURL: DASHBOARD_BASE_URL,
      rollupConfig: { external: [/^@sentry\//] },
    }),
    tailwindcss(),
    tanstackStart({ router: { basepath: DASHBOARD_BASE_PATH } }),
    viteReact(),
  ],
})

export default config
