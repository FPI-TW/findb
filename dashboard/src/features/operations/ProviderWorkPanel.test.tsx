import "@testing-library/jest-dom/vitest"
import { cleanup, render, screen } from "@testing-library/react"
import { afterEach, expect, it, vi } from "vitest"
import { ProviderWorkPanel } from "./ProviderWorkPanel"
import { providerWorkSchema } from "../../lib/admin-api"

const mocks = vi.hoisted(() => ({ query: vi.fn() }))
vi.mock("./delivery.queries", () => ({ useDeliveryResource: mocks.query }))
afterEach(cleanup)

it("shows loading before any response and disables hidden polling", () => {
  mocks.query.mockReturnValue({
    data: undefined,
    isFetching: true,
    error: null,
    dataUpdatedAt: 0,
    refetch: vi.fn(),
  })
  render(<ProviderWorkPanel active={false} />)
  expect(screen.getByText("載入供應商佇列…")).toBeInTheDocument()
  expect(
    screen.queryByText("目前沒有等待或執行中的工作。")
  ).not.toBeInTheDocument()
  expect(mocks.query).toHaveBeenCalledWith(
    expect.objectContaining({ resource: "providerWork" }),
    false
  )
})

it("shows whole-provider active work, FIFO position, retry timing and recovery heartbeat", () => {
  mocks.query.mockReturnValue({
    data: {
      resource: "providerWork",
      result: {
        ...providerWorkSchema.parse({
          data: [
            {
              work_id: "019f98b5-ee0b-7b16-9a28-d8e2ed638a91",
              provider: "taifex",
              dataset_key: "tw_futures_eod",
              kind: "historical",
              trade_date: "2026-10-15",
              execution_profile: null,
              state: "recovery_blocked",
              active: true,
              stable_key: "historical:fixture",
              queue_order: 1,
              drain_only: false,
              position: null,
              attempt_number: 2,
              not_before: "2026-10-19T01:00:00Z",
              heartbeat_at: "2026-10-19T00:59:00Z",
              wait_reason: "recovery_required",
            },
            {
              work_id: "019f98b5-ee0b-7b16-9a28-d8e2ed638a92",
              provider: "taifex",
              dataset_key: "tw_futures_eod",
              kind: "daily",
              trade_date: "2026-10-16",
              execution_profile: "pilot",
              state: "queued",
              active: false,
              stable_key: "daily:pilot:fixture",
              queue_order: 2,
              drain_only: true,
              position: null,
              attempt_number: 1,
              not_before: "2099-10-19T01:00:00Z",
              heartbeat_at: null,
              wait_reason: "not_before",
            },
            {
              work_id: "019f98b5-ee0b-7b16-9a28-d8e2ed638a93",
              provider: "twelve_data",
              dataset_key: "us_equity_eod",
              kind: "daily",
              trade_date: "2026-10-16",
              execution_profile: "pilot",
              state: "queued",
              active: false,
              stable_key: "daily:pilot:following",
              queue_order: 3,
              drain_only: false,
              position: 1,
              attempt_number: 1,
              not_before: "2026-10-19T01:00:00Z",
              heartbeat_at: null,
              wait_reason: null,
            },
          ],
        }),
      },
    },
    isFetching: false,
    error: null,
    dataUpdatedAt: Date.now(),
    refetch: vi.fn(),
  })
  render(<ProviderWorkPanel />)
  expect(
    screen.getByText("等待確認舊程序已停止並復原原工作")
  ).toBeInTheDocument()
  expect(screen.getByText("第 1 位")).toBeInTheDocument()
  expect(screen.getByText("第 2 輪")).toBeInTheDocument()
  expect(screen.getByText("2026-10-19T00:59:00Z")).toBeInTheDocument()
  expect(screen.getByText("僅排空原交付")).toBeInTheDocument()
  expect(screen.getByText("等待市場收盤或重試時間")).toBeInTheDocument()
  expect(screen.getByText("2099-10-19T01:00:00Z")).toBeInTheDocument()
})
