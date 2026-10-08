import {
  hashKey,
  useIsFetching,
  useQueryClient,
  type QueryKey,
} from "@tanstack/react-query"
import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useId,
  useMemo,
  useState,
  useRef,
  type ReactNode,
} from "react"

import { isDashboardAuthenticationError } from "../lib/auth-errors"

export const LIVE_REFRESH_MS = 60_000
export const manualQueryOptions = {
  retry: false,
  refetchOnWindowFocus: false,
  refetchOnReconnect: false,
} as const
export const liveQueryOptions = {
  staleTime: LIVE_REFRESH_MS,
  refetchInterval: LIVE_REFRESH_MS,
  refetchIntervalInBackground: false,
  refetchOnWindowFocus: true,
  refetchOnReconnect: true,
  retry: false,
} as const

const RefreshContext = createContext({
  register:
    (_id: string, _key: QueryKey): (() => void) =>
    () => {},
  refresh: async () => {},
  pending: false,
})

/** Exact, visible query registrations keep page refresh independent of feature key prefixes. */
export function OperationsRefreshProvider({
  children,
  onAuthenticationFailure,
}: {
  children: ReactNode
  onAuthenticationFailure: () => void
}) {
  const client = useQueryClient()
  const [keys, setKeys] = useState<Map<string, QueryKey>>(() => new Map())
  const [authenticationFailed, setAuthenticationFailed] = useState(false)
  const handlingAuthenticationFailure = useRef(false)
  const register = useCallback((id: string, key: QueryKey) => {
    setKeys(current => new Map(current).set(id, key))
    return () =>
      setKeys(current => {
        const next = new Map(current)
        next.delete(id)
        return next
      })
  }, [])
  const hashes = useMemo(() => new Set([...keys.values()].map(hashKey)), [keys])
  const pending =
    useIsFetching({ predicate: query => hashes.has(query.queryHash) }) > 0
  const refresh = useCallback(async () => {
    await client.refetchQueries(
      {
        type: "active",
        predicate: query => hashes.has(query.queryHash),
      },
      { cancelRefetch: false }
    )
  }, [client, hashes])

  useEffect(() => {
    const check = () => {
      if (handlingAuthenticationFailure.current) return
      const expired = client
        .getQueryCache()
        .findAll()
        .some(
          query =>
            hashes.has(query.queryHash) &&
            isDashboardAuthenticationError(query.state.error)
        )
      if (!expired) return
      handlingAuthenticationFailure.current = true
      setAuthenticationFailed(true)
      client.removeQueries({
        predicate: query =>
          ["operations", "calendar", "admin"].includes(
            String(query.queryKey[0])
          ),
      })
      onAuthenticationFailure()
    }
    check()
    return client.getQueryCache().subscribe(check)
  }, [client, hashes, onAuthenticationFailure])

  return (
    <RefreshContext.Provider value={{ register, refresh, pending }}>
      {authenticationFailed ? (
        <p role="status">登入已失效，正在返回登入頁…</p>
      ) : (
        children
      )}
    </RefreshContext.Provider>
  )
}

export function useRegisterOperationsQuery(queryKey: QueryKey, enabled = true) {
  const { register } = useContext(RefreshContext)
  const id = useId()
  // Stable serialization prevents registrations from changing on every data update.
  const serialized = hashKey(queryKey)
  useEffect(() => {
    if (!enabled) return
    return register(id, JSON.parse(serialized) as QueryKey)
  }, [enabled, id, register, serialized])
}

export function useOperationsRefresh() {
  return useContext(RefreshContext)
}
