import {
  QueryClient,
  QueryClientProvider,
  focusManager,
  onlineManager,
  useQuery,
  useMutation,
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
  prefix = "operations",
  enabled,
  load,
  manual = false,
}: {
  name: string
  prefix?: string
  enabled: boolean
  load: () => Promise<string>
  manual?: boolean
}) {
  const key = [prefix, name]
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

  it.each(["operations", "calendar", "admin"])(
    "handles late %s authentication expiry after its query is disabled and unregistered",
    async prefix => {
      vi.useFakeTimers()
      focusManager.setFocused(true)
      const client = new QueryClient()
      for (const scope of ["operations", "calendar", "admin"])
        client.setQueryData([scope, "cached"], "private")
      client.setQueryData(["public", "lookup"], "public")
      let reject!: (error: Error) => void
      const load = vi.fn(
        () =>
          new Promise<string>((_resolve, fail) => {
            reject = fail
          })
      )
      const navigate = vi.fn()
      const page = (enabled: boolean) => (
        <QueryClientProvider client={client}>
          <OperationsRefreshProvider onAuthenticationFailure={navigate}>
            <Refresh />
            <Consumer
              name="late"
              prefix={prefix}
              enabled={enabled}
              load={load}
            />
            <ExpiringAction />
          </OperationsRefreshProvider>
        </QueryClientProvider>
      )
      const { rerender } = render(page(true))
      expect(load).toHaveBeenCalledTimes(1)
      rerender(page(false))
      await act(() => vi.advanceTimersByTimeAsync(120_000))
      fireEvent.click(screen.getByText("Refresh"))
      await act(() => vi.advanceTimersByTimeAsync(1))
      expect(load).toHaveBeenCalledTimes(1)
      expect(navigate).not.toHaveBeenCalled()

      await act(async () => {
        reject(new DashboardAuthenticationError())
        await vi.advanceTimersByTimeAsync(1)
      })

      expect(screen.getByText("登入已失效，正在返回登入頁…")).toBeTruthy()
      expect(screen.queryByText("Refresh")).toBeNull()
      expect(screen.queryByText("Protected action")).toBeNull()
      for (const scope of ["operations", "calendar", "admin"])
        expect(
          client.getQueryCache().findAll({ queryKey: [scope] })
        ).toHaveLength(0)
      expect(client.getQueryData(["public", "lookup"])).toBe("public")
      expect(navigate).toHaveBeenCalledTimes(1)
    }
  )

  it.each(["public", "unrelated"])(
    "ignores authentication-shaped errors from %s queries",
    async prefix => {
      const client = new QueryClient()
      for (const scope of ["operations", "calendar", "admin"])
        client.setQueryData([scope, "cached"], "private")
      const error = new DashboardAuthenticationError()
      const navigate = vi.fn()
      render(
        <QueryClientProvider client={client}>
          <OperationsRefreshProvider onAuthenticationFailure={navigate}>
            <Refresh />
            <Consumer
              name="expired"
              prefix={prefix}
              enabled
              manual
              load={() => Promise.reject(error)}
            />
            <ExpiringAction />
          </OperationsRefreshProvider>
        </QueryClientProvider>
      )
      await act(async () => {
        await new Promise(resolve => setTimeout(resolve, 0))
      })
      expect(client.getQueryState([prefix, "expired"])?.error).toBe(error)
      for (const scope of ["operations", "calendar", "admin"])
        expect(client.getQueryData([scope, "cached"])).toBe("private")
      expect(screen.getByText("Refresh")).toBeTruthy()
      expect(screen.getByText("Protected action")).toBeTruthy()
      expect(navigate).not.toHaveBeenCalled()
    }
  )
})

function ExpiringAction({ mutation = false }: { mutation?: boolean }) {
  const { reportError } = useOperationsRefresh()
  const action = useMutation({
    mutationFn: () => Promise.reject(new DashboardAuthenticationError()),
  })
  return (
    <button
      onClick={() => {
        if (mutation) void action.mutateAsync().catch(() => {})
        else {
          reportError(new DashboardAuthenticationError())
          reportError(new DashboardAuthenticationError())
        }
      }}
    >
      Protected action
    </button>
  )
}

it.each([false, true])(
  "cancels unresolved protected queries and clears all protected caches for direct/mutation expiry (%s) with one navigation",
  async mutation => {
    const client = new QueryClient({
      defaultOptions: { queries: { retry: false } },
    })
    for (const prefix of ["operations", "calendar", "admin"])
      client.setQueryData([prefix, "cached"], "private")
    client.setQueryData(["public", "lookup"], "public")
    let resolve!: (value: string) => void
    const load = vi.fn(
      () =>
        new Promise<string>(done => {
          resolve = done
        })
    )
    const navigate = vi.fn()
    render(
      <QueryClientProvider client={client}>
        <OperationsRefreshProvider onAuthenticationFailure={navigate}>
          <Consumer name="late" enabled load={load} />
          <ExpiringAction mutation={mutation} />
        </OperationsRefreshProvider>
      </QueryClientProvider>
    )
    fireEvent.click(screen.getByText("Protected action"))
    await screen.findByText("登入已失效，正在返回登入頁…")
    await act(async () => {
      resolve("late private result")
      await Promise.resolve()
    })
    for (const prefix of ["operations", "calendar", "admin"])
      expect(
        client.getQueryCache().findAll({ queryKey: [prefix] })
      ).toHaveLength(0)
    expect(client.getQueryData(["public", "lookup"])).toBe("public")
    expect(screen.queryByText("Protected action")).toBeNull()
    expect(navigate).toHaveBeenCalledTimes(1)
  }
)

it("keeps normal permission errors visible without clearing the session", async () => {
  const client = new QueryClient()
  const navigate = vi.fn()
  function Action() {
    const { reportError } = useOperationsRefresh()
    return (
      <button onClick={() => reportError(new Error("沒有執行此操作的權限。"))}>
        Permission denied
      </button>
    )
  }
  render(
    <QueryClientProvider client={client}>
      <OperationsRefreshProvider onAuthenticationFailure={navigate}>
        <Action />
      </OperationsRefreshProvider>
    </QueryClientProvider>
  )
  client.setQueryData(["admin", "cached"], "private")
  fireEvent.click(screen.getByText("Permission denied"))
  expect(client.getQueryData(["admin", "cached"])).toBe("private")
  expect(navigate).not.toHaveBeenCalled()
})
