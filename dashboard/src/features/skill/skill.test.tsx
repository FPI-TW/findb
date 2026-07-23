import { fireEvent, render, screen, waitFor } from "@testing-library/react"
import { afterEach, describe, expect, it, vi } from "vitest"

import InstallTabs from "./InstallTabs"
import { copyTextToClipboard, detectOperatingSystem } from "./clipboard"

afterEach(() => {
  vi.restoreAllMocks()
  vi.unstubAllGlobals()
})

describe("skill install tabs", () => {
  it("exposes an accessible tab interface and switches panels by keyboard", () => {
    render(<InstallTabs />)

    const claudeTab = screen.getByRole("tab", { name: "Claude Code" })
    const codexTab = screen.getByRole("tab", { name: "Codex" })

    expect(claudeTab.getAttribute("aria-selected")).toBe("true")
    expect(screen.getByRole("tabpanel", { name: "Claude Code" }).hidden).toBe(
      false
    )

    claudeTab.focus()
    fireEvent.keyDown(claudeTab, { key: "ArrowRight" })

    expect(document.activeElement).toBe(codexTab)
    expect(codexTab.getAttribute("aria-selected")).toBe("true")
    expect(screen.getByRole("tabpanel", { name: "Codex" }).hidden).toBe(false)
  })

  it("announces successful clipboard copies", async () => {
    const writeText = vi.fn().mockResolvedValue(undefined)
    vi.stubGlobal("navigator", {
      ...window.navigator,
      clipboard: { writeText },
      userAgent: "Mozilla/5.0 (Macintosh)",
      platform: "MacIntel",
    })

    render(<InstallTabs />)
    fireEvent.click(
      screen.getByRole("button", {
        name: "複製 macOS · Linux · WSL 指令",
      })
    )

    await waitFor(() => {
      expect(writeText).toHaveBeenCalledWith(
        "mkdir -p ~/.claude/skills\nunzip findb-api.skill -d ~/.claude/skills/"
      )
      expect(screen.getByText("內容已複製到剪貼簿")).toBeTruthy()
    })
  })
})

describe("skill install browser helpers", () => {
  it("detects Windows separately from Unix-like systems", () => {
    expect(detectOperatingSystem("Mozilla/5.0", "Win32")).toBe("windows")
    expect(detectOperatingSystem("Mozilla/5.0 (X11; Linux)", "Linux")).toBe(
      "unix"
    )
  })

  it("falls back to document.execCommand when Clipboard API is unavailable", async () => {
    const execCommand = vi.fn().mockReturnValue(true)
    Object.defineProperty(document, "execCommand", {
      configurable: true,
      value: execCommand,
    })

    await expect(
      copyTextToClipboard(
        "fallback content",
        { writeText: vi.fn().mockRejectedValue(new Error("denied")) },
        document
      )
    ).resolves.toBe(true)
    expect(execCommand).toHaveBeenCalledWith("copy")
    expect(document.querySelector("textarea")).toBeNull()
  })
})
