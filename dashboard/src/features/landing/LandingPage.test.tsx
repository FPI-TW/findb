import { render, screen } from "@testing-library/react"
import type { ReactNode } from "react"
import { describe, expect, it, vi } from "vitest"

vi.mock("@tanstack/react-router", () => ({
  Link: ({
    children,
    className,
    to,
  }: {
    children: ReactNode
    className?: string
    to: string
  }) => (
    <a className={className} href={to}>
      {children}
    </a>
  ),
}))

import LandingPage from "./LandingPage"

describe("LandingPage workspace", () => {
  it("prioritizes Lookup and Operations over supporting resources", () => {
    render(<LandingPage />)

    expect(
      screen.getByRole("heading", { level: 1, name: "資料工作台" })
    ).toBeTruthy()
    expect(
      screen.getByRole("heading", { level: 2, name: "主要工作區" })
    ).toBeTruthy()
    expect(screen.getByRole("link", { name: /開啟 Lookup/ })).toBeTruthy()
    expect(screen.getByRole("link", { name: /進入 Operations/ })).toBeTruthy()

    const skillLink = screen.getByRole("link", { name: /查看 Skill/ })
    expect(skillLink.closest("aside")).not.toBeNull()
    expect(
      screen.getByRole("navigation", { name: "Operations 快捷入口" })
    ).toBeTruthy()
  })
})
