import "@testing-library/jest-dom/vitest"

import { act, cleanup, fireEvent, render, screen } from "@testing-library/react"
import { afterEach, describe, expect, it } from "vitest"

import { toast, Toaster } from "./toast"

afterEach(() => {
  act(() => toast.dismiss())
  cleanup()
})

describe("toast", () => {
  it("announces operation feedback and allows dismissal", () => {
    render(<Toaster />)

    act(() => {
      toast.success("使用者已建立", {
        description: "viewer 已建立。",
        duration: 0,
      })
    })

    expect(screen.getByRole("status")).toHaveTextContent("使用者已建立")
    expect(screen.getByRole("status")).toHaveTextContent("viewer 已建立。")

    fireEvent.click(screen.getByRole("button", { name: "關閉通知" }))
    expect(screen.queryByRole("status")).not.toBeInTheDocument()
  })

  it("uses an alert role for failures", () => {
    render(<Toaster />)

    act(() => {
      toast.error("撤銷 Credential 失敗", {
        description: "沒有執行此操作的權限。",
        duration: 0,
      })
    })

    expect(screen.getByRole("alert")).toHaveTextContent(
      "沒有執行此操作的權限。"
    )
  })
})
