import {
  Clipboard,
  LineChart,
  RefreshCw,
  TrendingDown,
  TrendingUp,
  X,
} from "lucide-react"
import { useCallback, useEffect, useMemo, useRef, useState } from "react"

import { Alert, AlertDescription } from "../../components/ui/alert"
import { Button } from "../../components/ui/button"
import { Skeleton } from "../../components/ui/skeleton"
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "../../components/ui/table"
import { loadCorporateActions, loadMacroObservations, loadPrices } from "./data"
import type {
  CorporateActionRow,
  DatasetKey,
  DetailFeedState,
  LookupItem,
  MacroObservationRow,
  PriceRow,
} from "./types"

function LoadingFeed() {
  return (
    <div className="space-y-2" role="status" aria-live="polite">
      <span className="sr-only">正在載入明細</span>
      <Skeleton className="h-4 w-full" />
      <Skeleton className="h-4 w-4/5" />
      <Skeleton className="h-4 w-3/5" />
    </div>
  )
}

function FeedError({ message, retry }: { message: string; retry: () => void }) {
  return (
    <Alert variant="destructive">
      <AlertDescription className="flex flex-wrap items-center justify-between gap-2">
        <span>{message}</span>
        <Button type="button" size="sm" variant="outline" onClick={retry}>
          <RefreshCw />
          重試
        </Button>
      </AlertDescription>
    </Alert>
  )
}

function Trend({ prices }: { prices: PriceRow[] }) {
  const values = [...prices]
    .reverse()
    .map(row => Number(row.close))
    .filter(Number.isFinite)
  if (values.length < 2) return null
  const minimum = Math.min(...values)
  const maximum = Math.max(...values)
  const range = maximum - minimum || 1
  const points = values
    .map((value, index) => {
      const x = 4 + (index * 112) / (values.length - 1)
      const y = 4 + 32 * (1 - (value - minimum) / range)
      return `${x},${y}`
    })
    .join(" ")
  const rising = (values.at(-1) ?? 0) >= (values[0] ?? 0)
  return (
    <div className="flex items-center gap-3 rounded-lg bg-surface-soft px-3 py-2">
      {rising ? (
        <TrendingUp className="text-accent" aria-hidden />
      ) : (
        <TrendingDown className="text-danger" aria-hidden />
      )}
      <svg
        className={
          rising ? "h-10 flex-1 text-accent" : "h-10 flex-1 text-danger"
        }
        viewBox="0 0 120 40"
        role="img"
        aria-label="最近收盤價趨勢"
      >
        <polyline
          points={points}
          fill="none"
          stroke="currentColor"
          strokeWidth="2"
          strokeLinecap="round"
          strokeLinejoin="round"
        />
      </svg>
    </div>
  )
}

function PriceFeed({
  state,
  retry,
}: {
  state: DetailFeedState<PriceRow>
  retry: () => void
}) {
  if (state.status === "loading") return <LoadingFeed />
  if (state.status === "error") {
    return <FeedError message={state.message} retry={retry} />
  }
  if (state.data.length === 0) {
    return <p className="text-sm text-muted">目前沒有價格紀錄。</p>
  }
  return (
    <div className="space-y-3">
      <Trend prices={state.data} />
      <Table>
        <TableHeader>
          <TableRow>
            <TableHead>交易日</TableHead>
            <TableHead>收盤價</TableHead>
            <TableHead>成交量</TableHead>
          </TableRow>
        </TableHeader>
        <TableBody>
          {state.data.map((row, index) => (
            <TableRow key={`${row.trade_date ?? "price"}-${index}`}>
              <TableCell>{row.trade_date ?? "—"}</TableCell>
              <TableCell>{row.close ?? "—"}</TableCell>
              <TableCell>{row.volume ?? "—"}</TableCell>
            </TableRow>
          ))}
        </TableBody>
      </Table>
    </div>
  )
}

function ActionFeed({
  state,
  retry,
}: {
  state: DetailFeedState<CorporateActionRow>
  retry: () => void
}) {
  if (state.status === "loading") return <LoadingFeed />
  if (state.status === "error") {
    return <FeedError message={state.message} retry={retry} />
  }
  if (state.data.length === 0) {
    return <p className="text-sm text-muted">目前沒有公司事件。</p>
  }
  return (
    <Table>
      <TableHeader>
        <TableRow>
          <TableHead>除權息日</TableHead>
          <TableHead>事件</TableHead>
          <TableHead>現金</TableHead>
        </TableRow>
      </TableHeader>
      <TableBody>
        {state.data.map((row, index) => (
          <TableRow key={`${row.ex_date ?? "action"}-${index}`}>
            <TableCell>{row.ex_date ?? "—"}</TableCell>
            <TableCell>{row.action_type ?? "—"}</TableCell>
            <TableCell>
              {row.cash_amount ?? "—"} {row.currency ?? ""}
            </TableCell>
          </TableRow>
        ))}
      </TableBody>
    </Table>
  )
}

function ObservationFeed({
  state,
  retry,
}: {
  state: DetailFeedState<MacroObservationRow>
  retry: () => void
}) {
  if (state.status === "loading") return <LoadingFeed />
  if (state.status === "error") {
    return <FeedError message={state.message} retry={retry} />
  }
  if (state.data.length === 0) {
    return <p className="text-sm text-muted">目前沒有觀測值。</p>
  }
  return (
    <Table>
      <TableHeader>
        <TableRow>
          <TableHead>觀測日</TableHead>
          <TableHead>數值</TableHead>
        </TableRow>
      </TableHeader>
      <TableBody>
        {state.data.map((row, index) => (
          <TableRow key={`${row.obs_date ?? "observation"}-${index}`}>
            <TableCell>{row.obs_date ?? "—"}</TableCell>
            <TableCell>{row.value ?? "—"}</TableCell>
          </TableRow>
        ))}
      </TableBody>
    </Table>
  )
}

export function DetailDrawer({
  dataset,
  item,
  onClose,
  onCopied,
}: {
  dataset: DatasetKey
  item: LookupItem | null
  onClose: () => void
  onCopied: (message: string) => void
}) {
  const drawerRef = useRef<HTMLElement>(null)
  const priorFocusRef = useRef<HTMLElement | null>(null)
  const [prices, setPrices] = useState<DetailFeedState<PriceRow>>({
    status: "loading",
    data: [],
  })
  const [actions, setActions] = useState<DetailFeedState<CorporateActionRow>>({
    status: "loading",
    data: [],
  })
  const [observations, setObservations] = useState<
    DetailFeedState<MacroObservationRow>
  >({ status: "loading", data: [] })

  const record = item as unknown as Record<string, unknown> | null
  const id = item
    ? dataset === "macro"
      ? String(record?.series_id ?? "")
      : String(record?.instrument_id ?? "")
    : ""

  const fetchPrices = useCallback(async () => {
    if (!id) return
    setPrices({ status: "loading", data: [] })
    try {
      setPrices({ status: "ready", data: await loadPrices(id) })
    } catch (error) {
      setPrices({
        status: "error",
        data: [],
        message: error instanceof Error ? error.message : "無法載入最近價格",
      })
    }
  }, [id])

  const fetchActions = useCallback(async () => {
    if (!id) return
    setActions({ status: "loading", data: [] })
    try {
      setActions({ status: "ready", data: await loadCorporateActions(id) })
    } catch (error) {
      setActions({
        status: "error",
        data: [],
        message: error instanceof Error ? error.message : "無法載入公司事件",
      })
    }
  }, [id])

  const fetchObservations = useCallback(async () => {
    if (!id) return
    setObservations({ status: "loading", data: [] })
    try {
      setObservations({
        status: "ready",
        data: await loadMacroObservations(id),
      })
    } catch (error) {
      setObservations({
        status: "error",
        data: [],
        message: error instanceof Error ? error.message : "無法載入觀測值",
      })
    }
  }, [id])

  useEffect(() => {
    if (!item) return
    priorFocusRef.current =
      document.activeElement instanceof HTMLElement
        ? document.activeElement
        : null
    window.requestAnimationFrame(() => drawerRef.current?.focus())
    if (dataset === "macro") {
      void fetchObservations()
    } else {
      void fetchPrices()
      void fetchActions()
    }
    return () => priorFocusRef.current?.focus()
  }, [dataset, fetchActions, fetchObservations, fetchPrices, item])

  const meta = useMemo(() => {
    if (!record) return []
    return dataset === "macro"
      ? [
          ["市場", record.market],
          ["頻率", record.frequency],
          ["單位", record.unit],
          ["來源", record.source],
          ["Series ID", record.series_id],
        ]
      : [
          ["市場", record.market],
          ["資產類別", record.asset_class],
          ["幣別", record.currency],
          ["狀態", record.status],
          ["Instrument ID", record.instrument_id],
        ]
  }, [dataset, record])

  if (!item || !record) return null
  const title =
    dataset === "macro"
      ? String(record.source_code || record.name || id)
      : String(record.symbol || id)
  const name = String(record.name || record.short_name || "")

  async function copy(value: unknown, label: string) {
    try {
      await navigator.clipboard.writeText(String(value ?? ""))
      onCopied(`${label}已複製`)
    } catch {
      onCopied("無法存取剪貼簿")
    }
  }

  function handleKeyDown(event: React.KeyboardEvent<HTMLElement>) {
    if (event.key === "Escape") {
      event.preventDefault()
      onClose()
      return
    }
    if (event.key !== "Tab") return
    const focusable = drawerRef.current?.querySelectorAll<HTMLElement>(
      'button:not([disabled]), [href], input:not([disabled]), select:not([disabled]), [tabindex]:not([tabindex="-1"])'
    )
    if (!focusable?.length) return
    const first = focusable[0]
    const last = focusable[focusable.length - 1]
    if (event.shiftKey && document.activeElement === first) {
      event.preventDefault()
      last?.focus()
    } else if (!event.shiftKey && document.activeElement === last) {
      event.preventDefault()
      first?.focus()
    }
  }

  return (
    <>
      <button
        type="button"
        className="fixed inset-0 z-40 cursor-default bg-black/35 backdrop-blur-xs"
        aria-label="關閉詳情"
        onClick={onClose}
      />
      <aside
        ref={drawerRef}
        className="fixed inset-y-0 right-0 z-50 flex w-full max-w-xl flex-col overflow-hidden border-l border-line bg-surface shadow-2xl outline-none"
        role="dialog"
        aria-modal="true"
        aria-labelledby="lookup-detail-title"
        tabIndex={-1}
        onKeyDown={handleKeyDown}
      >
        <header className="flex items-start justify-between gap-4 border-b border-line p-5">
          <div className="min-w-0">
            <p className="font-mono text-xs tracking-widest text-accent uppercase">
              {dataset === "macro" ? "Macro series" : "Instrument"}
            </p>
            <h2
              id="lookup-detail-title"
              className="mt-1 truncate text-xl font-semibold"
            >
              {title}
            </h2>
            <p className="mt-1 text-sm text-muted">{name}</p>
          </div>
          <Button
            type="button"
            variant="ghost"
            size="icon"
            aria-label="關閉詳情"
            onClick={onClose}
          >
            <X />
          </Button>
        </header>
        <div className="flex-1 space-y-6 overflow-y-auto p-5">
          <section aria-labelledby="lookup-meta-heading">
            <h3 id="lookup-meta-heading" className="mb-3 text-sm font-semibold">
              基本資訊
            </h3>
            <dl className="grid grid-cols-1 gap-2 sm:grid-cols-2">
              {meta.map(([label, value]) => (
                <div
                  key={String(label)}
                  className="rounded-lg bg-surface-soft px-3 py-2"
                >
                  <dt className="text-xs text-muted">{String(label)}</dt>
                  <dd className="mt-1 break-all font-mono text-sm">
                    {String(value || "—")}
                  </dd>
                </div>
              ))}
            </dl>
            <div className="mt-3 flex flex-wrap gap-2">
              <Button
                type="button"
                size="sm"
                variant="outline"
                onClick={() =>
                  void copy(
                    dataset === "macro" ? record.source_code : record.symbol,
                    dataset === "macro" ? "Source Code" : "Symbol"
                  )
                }
              >
                <Clipboard />
                複製 {dataset === "macro" ? "Source Code" : "Symbol"}
              </Button>
              <Button
                type="button"
                size="sm"
                variant="outline"
                onClick={() => void copy(id, "ID")}
              >
                <Clipboard />
                複製 ID
              </Button>
            </div>
          </section>

          {dataset === "macro" ? (
            <section aria-labelledby="lookup-observations-heading">
              <h3
                id="lookup-observations-heading"
                className="mb-3 flex items-center gap-2 text-sm font-semibold"
              >
                <LineChart size={16} />
                最近觀測值
              </h3>
              <ObservationFeed
                state={observations}
                retry={() => void fetchObservations()}
              />
            </section>
          ) : (
            <>
              <section aria-labelledby="lookup-prices-heading">
                <h3
                  id="lookup-prices-heading"
                  className="mb-3 flex items-center gap-2 text-sm font-semibold"
                >
                  <LineChart size={16} />
                  最近價格
                </h3>
                <PriceFeed state={prices} retry={() => void fetchPrices()} />
              </section>
              <section aria-labelledby="lookup-actions-heading">
                <h3
                  id="lookup-actions-heading"
                  className="mb-3 text-sm font-semibold"
                >
                  近期公司事件
                </h3>
                <ActionFeed state={actions} retry={() => void fetchActions()} />
              </section>
            </>
          )}
        </div>
      </aside>
    </>
  )
}
