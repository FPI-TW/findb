import { QueryClient, QueryClientProvider } from "@tanstack/react-query"
import type { ReactNode } from "react"
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"
import { page } from "vitest/browser"
import { cleanup, render } from "vitest-browser-react"

import "../../styles.css"
import type { Scheduler } from "../../lib/admin-api"

const mocks = vi.hoisted(() => ({
  updateScheduler: vi.fn(),
  loadDashboard: vi.fn(),
  loadRawPayloadDetail: vi.fn(),
  navigate: vi.fn(),
}))

vi.mock("@tanstack/react-router", () => ({
  Link: ({ children }: { children: ReactNode }) => <>{children}</>,
  Outlet: () => null,
  useNavigate: () => mocks.navigate,
}))
vi.mock("@tanstack/react-start", () => ({
  useServerFn: (serverFn: unknown) => serverFn,
}))
vi.mock("../../lib/admin.functions", () => mocks)
vi.mock("../../lib/auth.functions", () => ({ logout: vi.fn() }))

import { IngestionOverviewPanel } from "./operations.overview"

const providers = ["finlab", "shioaji", "taifex", "twelve_data"]
const timestamp = "2026-08-04T02:00:00Z"

function scheduler(provider: string, fullMarket: boolean): Scheduler {
  return {
    scheduler_key: fullMarket
      ? `full_market_${provider}_v1`
      : `pilot_${provider}`,
    provider,
    dataset_keys: ["tw_equity_eod"],
    slot_id: "taiwan_market_window",
    scheduled_local_time: "14:30:00",
    timezone: "Asia/Taipei",
    desired_state: fullMarket ? "running" : "stopped",
    observed_state: "stopped",
    start_allowed: true,
    revision: 1,
    last_heartbeat_at: timestamp,
    last_cycle_started_at: timestamp,
    last_cycle_completed_at: timestamp,
    last_error: null,
    created_at: timestamp,
    updated_at: timestamp,
    heartbeat_age_seconds: 5,
  }
}

function dialogElement() {
  const dialog = document.querySelector<HTMLElement>('[role="alertdialog"]')
  expect(dialog).not.toBeNull()
  return dialog!
}

function expectWithinViewport(element: HTMLElement) {
  const bounds = element.getBoundingClientRect()
  expect(bounds.top).toBeGreaterThanOrEqual(0)
  expect(bounds.bottom).toBeLessThanOrEqual(window.innerHeight)
  expect(bounds.left).toBeGreaterThanOrEqual(0)
  expect(bounds.right).toBeLessThanOrEqual(window.innerWidth)
}

function revealAction(dialog: HTMLElement, name: string) {
  const button = Array.from(dialog.querySelectorAll("button")).find(
    candidate => candidate.textContent === name
  )
  expect(button).toBeDefined()
  dialog.scrollTop = dialog.scrollHeight
  button!.scrollIntoView({ block: "nearest" })
  expectWithinViewport(button!)
  const dialogBounds = dialog.getBoundingClientRect()
  const buttonBounds = button!.getBoundingClientRect()
  expect(buttonBounds.top).toBeGreaterThanOrEqual(dialogBounds.top)
  expect(buttonBounds.bottom).toBeLessThanOrEqual(dialogBounds.bottom)
}

beforeEach(async () => {
  vi.clearAllMocks()
  await page.viewport(360, 640)
  mocks.updateScheduler.mockImplementation(
    async ({ data }: { data: { schedulerKey: string } }) => ({
      success: true,
      data: {
        ...scheduler(data.schedulerKey.replace("pilot_", ""), false),
        desired_state: "running",
        revision: 2,
      },
    })
  )
})

afterEach(async () => {
  cleanup()
  await page.viewport(1440, 900)
})

describe("scheduler confirmation mobile layout", () => {
  it.each(["single", "bulk"] as const)(
    "keeps the %s warning, targets, cancel and confirm usable at 360x640",
    async mode => {
      const rows = providers.flatMap(provider => [
        scheduler(provider, false),
        scheduler(provider, true),
      ])
      const screen = await render(
        <QueryClientProvider client={new QueryClient()}>
          <IngestionOverviewPanel
            freshnessResult={null}
            schedulersResult={{ ok: true, data: { success: true, data: rows } }}
            loading={false}
            pending={false}
            freshnessError=""
            schedulersError=""
            role="owner"
          />
        </QueryClientProvider>
      )
      const trigger = screen.getByRole("button", {
        name:
          mode === "bulk"
            ? "Pilot 全部啟動"
            : "Pilot finlab pilot_finlab 設為執行中",
      })
      await trigger.click()
      const dialog = dialogElement()
      expectWithinViewport(dialog)
      expect(dialog.textContent).toContain("Full market 與 Pilot 將同時執行")
      const warningTitle = dialog.querySelector<HTMLElement>(
        '[data-slot="alert-title"]'
      )!
      warningTitle.scrollIntoView({ block: "nearest" })
      expectWithinViewport(warningTitle)
      const overlapProviders = mode === "bulk" ? providers : ["finlab"]
      for (const provider of overlapProviders) {
        expect(dialog.textContent).toContain(`full_market_${provider}_v1`)
      }
      if (mode === "bulk") {
        for (const provider of providers) {
          expect(dialog.textContent).toContain(`pilot_${provider}`)
        }
        expect(dialog.scrollHeight).toBeGreaterThan(dialog.clientHeight)
      }
      for (const item of dialog.querySelectorAll<HTMLElement>("li")) {
        item.scrollIntoView({ block: "nearest" })
        expectWithinViewport(item)
      }
      expect(mocks.updateScheduler).not.toHaveBeenCalled()
      revealAction(dialog, "取消")
      await page.getByRole("button", { name: "取消", exact: true }).click()
      expect(document.querySelector('[role="alertdialog"]')).toBeNull()
      expect(mocks.updateScheduler).not.toHaveBeenCalled()

      await trigger.click()
      const confirmName = mode === "bulk" ? "確認 Pilot 全部啟動" : "確認啟用"
      const reopenedDialog = dialogElement()
      expectWithinViewport(reopenedDialog)
      revealAction(reopenedDialog, confirmName)
      await page.getByRole("button", { name: confirmName, exact: true }).click()
      await expect
        .poll(() => mocks.updateScheduler.mock.calls.length)
        .toBe(mode === "bulk" ? 4 : 1)
      expect(document.querySelector('[role="alertdialog"]')).toBeNull()
      expect(
        mocks.updateScheduler.mock.calls.map(([request]) => request.data)
      ).toEqual(
        (mode === "bulk" ? providers : ["finlab"]).map(provider => ({
          schedulerKey: `pilot_${provider}`,
          desiredState: "running",
          expectedRevision: 1,
        }))
      )
    }
  )
})
