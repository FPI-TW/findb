import "@testing-library/jest-dom/vitest"

import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react"
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"

import { toast, Toaster } from "../../components/ui/toast"

const mocks = vi.hoisted(() => ({
  loadCredentials: vi.fn(),
  loadCredentialsOverview: vi.fn(),
  issueCredential: vi.fn(),
  rotateCredential: vi.fn(),
  revokeCredential: vi.fn(),
  loadUsers: vi.fn(),
  createUser: vi.fn(),
  updateUser: vi.fn(),
  resetUserPassword: vi.fn(),
}))

vi.mock("@tanstack/react-start", () => ({
  useServerFn: (serverFn: unknown) => serverFn,
}))

vi.mock("../../lib/admin-governance.functions", () => mocks)

import { CredentialsPage } from "./CredentialsPage"
import { UsersPage } from "./UsersPage"

const timestamp = "2026-07-28T02:00:00Z"
const credential = {
  credential_ref: "serve:019565d2-f838-7c91-85c1-72d4d7bbbe98",
  id: "019565d2-f838-7c91-85c1-72d4d7bbbe98",
  kind: "serve" as const,
  name: "lookup",
  owner: "web",
  description: null,
  status: "active" as const,
  fingerprint: "sha256:abcd",
  role: null,
  scopes: ["eod:read"],
  policies: {},
  created_at: timestamp,
  expires_at: null,
  revoked_at: null,
  last_used_at: timestamp,
  usage_count: 42,
  rotated_from_id: null,
}
const user = {
  user_id: "019565d2-f838-7c91-85c1-72d4d7bbbe97",
  username: "owner",
  display_name: "Primary Owner",
  role: "owner" as const,
  is_active: true,
  must_change_password: false,
  last_login_at: timestamp,
}

beforeEach(() => {
  vi.clearAllMocks()
  mocks.loadCredentials.mockResolvedValue({ data: [credential] })
  mocks.loadCredentialsOverview.mockResolvedValue({
    auth_mode: "db_only",
    serve_require_auth: true,
    counts: {
      active: 1,
      expiring: 0,
      expired: 0,
      revoked: 0,
    },
    usage_updated_at: timestamp,
  })
  mocks.loadUsers.mockResolvedValue({ data: [user] })
})

afterEach(() => {
  toast.dismiss()
  cleanup()
  vi.restoreAllMocks()
})

describe("governance pages", () => {
  it("keeps credential mutation controls hidden from viewers", async () => {
    render(<CredentialsPage role="viewer" />)

    expect(await screen.findByText("lookup")).toBeInTheDocument()
    expect(screen.queryByText("簽發 credential")).not.toBeInTheDocument()
    expect(screen.queryByLabelText("輪替 lookup")).not.toBeInTheDocument()
    expect(screen.queryByLabelText("撤銷 lookup")).not.toBeInTheDocument()
  })

  it("issues a source credential and displays its secret once", async () => {
    mocks.issueCredential.mockResolvedValue({
      api_key: "findb_src_one_time",
      data: { ...credential, kind: "source", name: "fetcher" },
    })
    render(<CredentialsPage role="owner" />)

    await screen.findByText("lookup")
    fireEvent.change(screen.getByLabelText("名稱"), {
      target: { value: "fetcher" },
    })
    fireEvent.change(screen.getByLabelText("Owner"), {
      target: { value: "data-platform" },
    })
    fireEvent.change(screen.getByLabelText("Source name"), {
      target: { value: "twelve_data" },
    })
    fireEvent.click(screen.getByRole("button", { name: "簽發" }))

    expect(await screen.findByText("findb_src_one_time")).toBeInTheDocument()
    expect(screen.getByText("此密鑰只會顯示一次")).toBeInTheDocument()
    expect(mocks.issueCredential).toHaveBeenCalledWith({
      data: expect.objectContaining({
        kind: "source",
        name: "fetcher",
        owner: "data-platform",
        source_name: "twelve_data",
        allowed_datasets: ["us_equity_eod"],
      }),
    })
    fireEvent.click(screen.getByRole("button", { name: "關閉" }))
    expect(screen.queryByText("findb_src_one_time")).not.toBeInTheDocument()
  })

  it("shows the exact provider governance datasets", async () => {
    render(<CredentialsPage role="owner" />)

    await screen.findByText("lookup")
    expect(screen.getByText("us_equity_eod")).toBeInTheDocument()
    expect(screen.getByRole("button", { name: "簽發" })).toBeEnabled()

    fireEvent.change(screen.getByLabelText("Source name"), {
      target: { value: "finlab" },
    })
    expect(screen.queryByText("us_equity_eod")).not.toBeInTheDocument()
    expect(screen.getByText("tw_equity_eod")).toBeInTheDocument()
    expect(screen.getByRole("button", { name: "簽發" })).toBeEnabled()

    expect(screen.getByRole("option", { name: "shioaji" })).toBeInTheDocument()
    fireEvent.change(screen.getByLabelText("Source name"), {
      target: { value: "shioaji" },
    })
    expect(screen.queryByText("tw_equity_eod")).not.toBeInTheDocument()
    expect(screen.getByText("tw_equity_minute")).toBeInTheDocument()
    expect(screen.getByText("tw_etf_minute")).toBeInTheDocument()
  })

  it("issues a source credential with the provider's exact governed datasets", async () => {
    mocks.issueCredential.mockResolvedValue({
      api_key: "findb_src_finlab",
      data: {
        ...credential,
        kind: "source",
        name: "finlab-fetcher",
        scopes: ["tw_equity_eod"],
        policies: {
          source_name: "finlab",
          allowed_datasets: ["tw_equity_eod"],
        },
      },
    })
    render(<CredentialsPage role="owner" />)

    await screen.findByText("lookup")
    fireEvent.change(screen.getByLabelText("名稱"), {
      target: { value: "finlab-fetcher" },
    })
    fireEvent.change(screen.getByLabelText("Owner"), {
      target: { value: "data-platform" },
    })
    fireEvent.change(screen.getByLabelText("Source name"), {
      target: { value: "finlab" },
    })
    expect(screen.getByRole("button", { name: "簽發" })).toBeEnabled()
    fireEvent.click(screen.getByRole("button", { name: "簽發" }))

    await waitFor(() =>
      expect(mocks.issueCredential).toHaveBeenCalledWith({
        data: expect.objectContaining({
          kind: "source",
          source_name: "finlab",
          allowed_datasets: ["tw_equity_eod"],
        }),
      })
    )
  })

  it("rotates with one-time display and confirms immediate revocation", async () => {
    mocks.rotateCredential.mockResolvedValue({
      api_key: "findb_srv_rotated_once",
      data: credential,
    })
    mocks.revokeCredential.mockResolvedValue({
      ...credential,
      status: "revoked",
    })
    vi.spyOn(window, "confirm").mockReturnValue(true)
    render(<CredentialsPage role="owner" />)

    await screen.findByText("lookup")
    fireEvent.click(screen.getByLabelText("輪替 lookup"))
    expect(
      await screen.findByText("findb_srv_rotated_once")
    ).toBeInTheDocument()
    fireEvent.click(screen.getByRole("button", { name: "關閉" }))
    fireEvent.click(screen.getByLabelText("撤銷 lookup"))

    await waitFor(() =>
      expect(mocks.revokeCredential).toHaveBeenCalledWith({
        data: { kind: "serve", id: credential.id },
      })
    )
    expect(window.confirm).toHaveBeenCalledWith(
      expect.stringContaining("下一個請求即失效")
    )
  })

  it("creates a user and displays the temporary password once", async () => {
    mocks.createUser.mockResolvedValue({
      data: { ...user, username: "viewer", role: "viewer" },
      temporary_password: "temporary-once",
    })
    render(<UsersPage />)

    await screen.findByText("Primary Owner")
    fireEvent.change(screen.getByLabelText("Username"), {
      target: { value: "viewer" },
    })
    fireEvent.change(screen.getByLabelText("顯示名稱"), {
      target: { value: "Read Only" },
    })
    fireEvent.click(screen.getByRole("button", { name: "建立" }))

    expect(await screen.findByText("temporary-once")).toBeInTheDocument()
    await waitFor(() =>
      expect(mocks.createUser).toHaveBeenCalledWith({
        data: expect.objectContaining({
          username: "viewer",
          display_name: "Read Only",
          role: "viewer",
          must_change_password: true,
        }),
      })
    )
  })

  it("uses non-login autocomplete tokens and omits an empty password", async () => {
    mocks.createUser.mockResolvedValue({
      data: { ...user, username: "viewer", role: "viewer" },
      temporary_password: "generated-once",
    })
    render(<UsersPage />)

    await screen.findByText("Primary Owner")
    const usernameInput = screen.getByLabelText("Username")
    const passwordInput = screen.getByLabelText("指定密碼（留空自動產生）")
    expect(usernameInput).toHaveAttribute("autocomplete", "off")
    expect(passwordInput).toHaveAttribute("autocomplete", "new-password")
    expect(usernameInput.closest("form")).toHaveAttribute("autocomplete", "off")

    fireEvent.change(usernameInput, { target: { value: "viewer" } })
    fireEvent.change(screen.getByLabelText("顯示名稱"), {
      target: { value: "Read Only" },
    })
    fireEvent.click(screen.getByRole("button", { name: "建立" }))

    await waitFor(() =>
      expect(mocks.createUser).toHaveBeenCalledWith({
        data: expect.objectContaining({
          username: "viewer",
          display_name: "Read Only",
          role: "viewer",
          must_change_password: true,
        }),
      })
    )
    const request = mocks.createUser.mock.calls.at(-1)?.[0] as {
      data?: { password?: string }
    }
    expect(request.data).not.toHaveProperty("password")
  })

  it("confirms password reset and shows the returned temporary password", async () => {
    mocks.resetUserPassword.mockResolvedValue({
      data: { ...user, must_change_password: true },
      temporary_password: "reset-once",
    })
    vi.spyOn(window, "confirm").mockReturnValue(true)
    render(<UsersPage />)

    await screen.findByText("Primary Owner")
    fireEvent.click(screen.getByLabelText("重設 owner 的密碼"))

    expect(await screen.findByText("reset-once")).toBeInTheDocument()
    expect(mocks.resetUserPassword).toHaveBeenCalledWith({
      data: { userId: user.user_id },
    })
  })

  it("shows mutation failures near the action as a toast", async () => {
    mocks.updateUser.mockRejectedValue(new Error("角色不可變更。"))
    render(
      <>
        <UsersPage />
        <Toaster />
      </>
    )

    await screen.findByText("Primary Owner")
    fireEvent.change(screen.getByLabelText("owner 的角色"), {
      target: { value: "viewer" },
    })

    expect(await screen.findByRole("alert")).toHaveTextContent("角色更新失敗")
    expect(screen.getByRole("alert")).toHaveTextContent("角色不可變更。")
    expect(screen.queryByText("操作失敗")).not.toBeInTheDocument()
  })
})
