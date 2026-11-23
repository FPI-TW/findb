import { readdirSync, readFileSync, statSync } from "node:fs"
import { join } from "node:path"
import { describe, expect, it } from "vitest"

// Colour pairs the components put together, checked in both themes against the
// tokens in styles.css, and a scan for "ink" text placed on anything but its own
// solid fill (white or near-black text on a soft background is unreadable).

// Vitest runs from the dashboard package; jsdom gives no file: URL for this module.
const SRC = join(process.cwd(), "src")
const css = readFileSync(join(SRC, "styles.css"), "utf8")

function themeTokens(selector: string): Record<string, string> {
  const start = css.indexOf(`${selector} {`)
  const block = css.slice(start, css.indexOf("}", start))
  return Object.fromEntries(
    [...block.matchAll(/--([a-z-]+):\s*(#[0-9a-f]{6})/gi)].map(match => [
      match[1],
      match[2],
    ])
  )
}

function luminance(hex: string): number {
  const channel = (offset: number) => {
    const value = parseInt(hex.slice(offset, offset + 2), 16) / 255
    return value <= 0.03928 ? value / 12.92 : ((value + 0.055) / 1.055) ** 2.4
  }
  return 0.2126 * channel(1) + 0.7152 * channel(3) + 0.0722 * channel(5)
}

function contrast(a: string | undefined, b: string | undefined): number {
  if (!a || !b) throw new Error("colour token is not defined")
  const high = Math.max(luminance(a), luminance(b))
  const low = Math.min(luminance(a), luminance(b))
  return (high + 0.05) / (low + 0.05)
}

// [foreground, background] as the components use them.
const TEXT_PAIRS: [string, string][] = [
  ["ink", "bg"],
  ["ink", "surface"],
  ["ink", "surface-soft"],
  ["ink", "accent-soft"],
  ["muted", "bg"],
  ["muted", "surface"],
  ["muted", "surface-soft"],
  ["accent", "surface"],
  ["accent", "surface-soft"],
  ["accent", "accent-soft"],
  ["accent-ink", "accent"],
  ["danger", "surface"],
  ["danger", "danger-soft"],
  ["danger-ink", "danger"],
  ["warning", "surface"],
  ["warning", "warning-soft"],
]

describe.each([
  ["light", ":root"],
  ["dark", ".dark"],
])("%s theme colours", (_theme, selector) => {
  const tokens = themeTokens(selector)

  it.each(TEXT_PAIRS)("%s text on %s is at least 4.5:1", (fg, bg) => {
    expect(tokens[fg], fg).toBeDefined()
    expect(tokens[bg], bg).toBeDefined()
    expect(contrast(tokens[fg], tokens[bg])).toBeGreaterThanOrEqual(4.5)
  })

  it("control borders are at least 3:1 on a card", () => {
    expect(
      contrast(tokens["line-strong"], tokens.surface)
    ).toBeGreaterThanOrEqual(3)
  })
})

function sourceFiles(dir: string): string[] {
  return readdirSync(dir).flatMap(name => {
    const path = join(dir, name)
    if (statSync(path).isDirectory()) return sourceFiles(path)
    return /\.tsx$/.test(name) && !/\.test\.tsx$/.test(name) ? [path] : []
  })
}

describe("ink text", () => {
  it("sits only on its own solid fill, in the same state", () => {
    const misuses: string[] = []
    for (const file of sourceFiles(SRC)) {
      const source = readFileSync(file, "utf8")
      for (const literal of source.match(/"[^"\n]*-ink[^"\n]*"/g) ?? []) {
        const classes = literal.slice(1, -1).split(/\s+/)
        for (const token of classes) {
          const match = token.match(
            /^((?:[a-z-]+(?:\[[^\]]+\])?:)*)text-(accent|danger)-ink$/
          )
          if (!match) continue
          const [, state, colour] = match
          if (!classes.includes(`${state}bg-${colour}`)) {
            misuses.push(`${file.slice(SRC.length)}: ${token}`)
          }
        }
      }
    }
    expect(misuses).toEqual([])
  })
})
