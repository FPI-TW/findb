import {
  QueryClient,
  QueryClientProvider,
  focusManager,
  onlineManager,
  useQuery,
} from "@tanstack/react-query"
import { act, cleanup, fireEvent, render, screen } from "@testing-library/react"
import { afterEach, describe, expect, it, vi } from "vitest"
import {
  OperationsRefreshProvider,
  liveQueryOptions,
  manualQueryOptions,
  useOperationsRefresh,
  useRegisterOperationsQuery,
} from "./OperationsRefresh"
import { DashboardAuthenticationError } from "../lib/auth-errors"

function Consumer({
  name,
  enabled,
  load,
  manual = false,
}: {
  name: string
  enabled: boolean
  load: () => Promise<string>
  manual?: boolean
}) {
  const key = ["operations", name]
  useRegisterOperationsQuery(key, enabled)
  const query = useQuery({
    ...(manual ? manualQueryOptions : liveQueryOptions),
    queryKey: key,
    queryFn: load,
    enabled,
  })
  return <span>{query.data}</span>
}
function Refresh() {
  const { refresh } = useOperationsRefresh()
  return <button onClick={() => void refresh()}>Refresh</button>
}
afterEach(() => {
  cleanup()
  focusManager.setFocused(undefined)
  onlineManager.setOnline(true)
  vi.useRealTimers()
})

describe("visible operations refresh", () => {
  it("keeps manual resources idle through elapsed time, focus and reconnect until page refresh", async () => {
    vi.useFakeTimers()
    const client = new QueryClient()
    const load = vi.fn().mockResolvedValue("manual")
    render(
      <QueryClientProvider client={client}>
        <OperationsRefreshProvider onAuthenticationFailure={() => {}}>
          <Refresh />
          <Consumer name="manual" enabled manual load={load} />
        </OperationsRefreshProvider>
      </QueryClientProvider>
    )
    await act(() => vi.advanceTimersByTimeAsync(1))
    act(() => {
      focusManager.setFocused(false)
      onlineManager.setOnline(false)
    })
    await act(() => vi.advanceTimersByTimeAsync(120_000))
    await act(async () => {
      focusManager.setFocused(true)
      onlineManager.setOnline(true)
      await vi.advanceTimersByTimeAsync(1)
    })
    expect(load).toHaveBeenCalledTimes(1)
    fireEvent.click(screen.getByText("Refresh"))
    await act(() => vi.advanceTimersByTimeAsync(1))
    expect(load).toHaveBeenCalledTimes(2)
  })
  it("deduplicates refresh, polls only visible enabled resources and refreshes stale data on focus", async () => {
    vi.useFakeTimers()
    focusManager.setFocused(true)
    const client = new QueryClient()
    const load = vi.fn().mockResolvedValue("current")
    const hidden = vi.fn().mockResolvedValue("hidden")
    render(
      <QueryClientProvider client={client}>
        <OperationsRefreshProvider onAuthenticationFailure={() => {}}>
          <Refresh />
          <Consumer name="current" enabled load={load} />
          <Consumer name="hidden" enabled={false} load={hidden} />
        </OperationsRefreshProvider>
      </QueryClientProvider>
    )
    await act(() => vi.advanceTimersByTimeAsync(1))
    expect(load).toHaveBeenCalledTimes(1)
    await act(() => vi.advanceTimersByTimeAsync(60_000))
    expect(load).toHaveBeenCalledTimes(2)
    focusManager.setFocused(false)
    await act(() => vi.advanceTimersByTimeAsync(120_000))
    expect(load).toHaveBeenCalledTimes(2)
    await act(async () => {
      focusManager.setFocused(true)
      await vi.advanceTimersByTimeAsync(1)
    })
    expect(load).toHaveBeenCalledTimes(3)
    let resolve!: (value: string) => void
    load.mockImplementation(
      () =>
        new Promise<string>(done => {
          resolve = done
        })
    )
    fireEvent.click(screen.getByText("Refresh"))
    fireEvent.click(screen.getByText("Refresh"))
    expect(load).toHaveBeenCalledTimes(4)
    await act(async () => {
      resolve("current")
      await vi.advanceTimersByTimeAsync(1)
    })
    expect(hidden).not.toHaveBeenCalled()
  })
  it("removes protected caches and hides actions on authentication expiry", async () => {
    const client = new QueryClient({
      defaultOptions: { queries: { retry: false } },
    })
    client.setQueryData(["admin", "other"], ["private"])
    const expired = vi.fn()
    render(
      <QueryClientProvider client={client}>
        <OperationsRefreshProvider onAuthenticationFailure={expired}>
          <Refresh />
          <Consumer
            name="expired"
            enabled
            load={() => Promise.reject(new DashboardAuthenticationError())}
          />
        </OperationsRefreshProvider>
      </QueryClientProvider>
    )
    await screen.findByText("登入已失效，正在返回登入頁…")
    expect(screen.queryByText("Refresh")).toBeNull()
    expect(client.getQueryData(["admin", "other"])).toBeUndefined()
    expect(expired).toHaveBeenCalledTimes(1)
  })
})
