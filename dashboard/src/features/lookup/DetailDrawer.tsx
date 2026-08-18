import { useQuery } from "@tanstack/react-query"
import type { ColumnDef } from "@tanstack/react-table"
import {
  Clipboard,
  LineChart,
  RefreshCw,
  TrendingDown,
  TrendingUp,
  X,
} from "lucide-react"
import { useEffect, useMemo, useRef } from "react"

import { DataTable } from "../../components/data-table"
import { Alert, AlertDescription } from "../../components/ui/alert"
import { Button } from "../../components/ui/button"
import { Skeleton } from "../../components/ui/skeleton"
import { loadCorporateActions, loadMacroObservations, loadPrices } from "./data"
import type {
  CorporateActionRow,
  DatasetKey,
  LookupItem,
  MacroObservationRow,
  PriceRow,
} from "./types"

function LoadingFeed() {
  return (
    <div className="space-y-2">
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

function errorMessage(error: unknown, fallback: string) {
  return error instanceof Error && error.message ? error.message : fallback
}

const PRICE_COLUMNS: ColumnDef<PriceRow, unknown>[] = [
  {
    accessorKey: "trade_date",
    header: "交易日",
    meta: { width: 124 },
    cell: ({ row }) => row.original.trade_date ?? "—",
  },
  {
    accessorKey: "close",
    header: "收盤價",
    meta: { width: 112, align: "right" },
    cell: ({ row }) => row.original.close ?? "—",
  },
  {
    accessorKey: "volume",
    header: "成交量",
    meta: { width: 112, align: "right" },
    cell: ({ row }) => row.original.volume ?? "—",
  },
]

const ACTION_COLUMNS: ColumnDef<CorporateActionRow, unknown>[] = [
  {
    accessorKey: "ex_date",
    header: "除權息日",
    meta: { width: 124 },
    cell: ({ row }) => row.original.ex_date ?? "—",
  },
  {
    accessorKey: "action_type",
    header: "事件",
    meta: { width: 112 },
    cell: ({ row }) => row.original.action_type ?? "—",
  },
  {
    id: "cash",
    accessorFn: row => row.cash_amount,
    header: "現金",
    meta: { width: 124, align: "right" },
    cell: ({ row }) =>
      `${row.original.cash_amount ?? "—"} ${row.original.currency ?? ""}`,
  },
]

const OBSERVATION_COLUMNS: ColumnDef<MacroObservationRow, unknown>[] = [
  {
    accessorKey: "obs_date",
    header: "觀測日",
    meta: { width: 124 },
    cell: ({ row }) => row.original.obs_date ?? "—",
  },
  {
    accessorKey: "value",
    header: "數值",
    meta: { width: 124, align: "right" },
    cell: ({ row }) => row.original.value ?? "—",
  },
]

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
  data,
  isLoading,
  isRefreshing,
  error,
  retry,
}: {
  data: PriceRow[]
  isLoading: boolean
  isRefreshing: boolean
  error: unknown
  retry: () => void
}) {
  return (
    <div className="space-y-3">
      {data.length > 0 ? <Trend prices={data} /> : null}
      <DataTable
        ariaLabel="最近價格"
        caption="最近價格"
        columns={PRICE_COLUMNS}
        data={data}
        emptyState="目前沒有價格紀錄。"
        error={error}
        errorState={
          <FeedError
            message={errorMessage(error, "無法載入最近價格")}
            retry={retry}
          />
        }
        getRowId={(row, index) => `${row.trade_date ?? "price"}-${index}`}
        isLoading={isLoading}
        isRefreshing={isRefreshing}
        loadingState={<LoadingFeed />}
        tableClassName="min-w-[360px]"
      />
    </div>
  )
}

function ActionFeed({
  data,
  isLoading,
  isRefreshing,
  error,
  retry,
}: {
  data: CorporateActionRow[]
  isLoading: boolean
  isRefreshing: boolean
  error: unknown
  retry: () => void
}) {
  return (
    <DataTable
      ariaLabel="近期公司事件"
      caption="近期公司事件"
      columns={ACTION_COLUMNS}
      data={data}
      emptyState="目前沒有公司事件。"
      error={error}
      errorState={
        <FeedError
          message={errorMessage(error, "無法載入公司事件")}
          retry={retry}
        />
      }
      getRowId={(row, index) => `${row.ex_date ?? "action"}-${index}`}
      isLoading={isLoading}
      isRefreshing={isRefreshing}
      loadingState={<LoadingFeed />}
      tableClassName="min-w-[360px]"
    />
  )
}

function ObservationFeed({
  data,
  isLoading,
  isRefreshing,
  error,
  retry,
}: {
  data: MacroObservationRow[]
  isLoading: boolean
  isRefreshing: boolean
  error: unknown
  retry: () => void
}) {
  return (
    <DataTable
      ariaLabel="最近觀測值"
      caption="最近觀測值"
      columns={OBSERVATION_COLUMNS}
      data={data}
      emptyState="目前沒有觀測值。"
      error={error}
      errorState={
        <FeedError
          message={errorMessage(error, "無法載入觀測值")}
          retry={retry}
        />
      }
      getRowId={(row, index) => `${row.obs_date ?? "observation"}-${index}`}
      isLoading={isLoading}
      isRefreshing={isRefreshing}
      loadingState={<LoadingFeed />}
      tableClassName="min-w-[260px]"
    />
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

  const record = item as unknown as Record<string, unknown> | null
  const id = item
    ? dataset === "macro"
      ? String(record?.series_id ?? "")
      : String(record?.instrument_id ?? "")
    : ""

  const pricesQuery = useQuery({
    queryKey: ["lookup-detail", "prices", id],
    queryFn: ({ signal }) => loadPrices(id, signal),
    enabled: Boolean(item && dataset === "instruments" && id),
  })
  const actionsQuery = useQuery({
    queryKey: ["lookup-detail", "corporate-actions", id],
    queryFn: ({ signal }) => loadCorporateActions(id, signal),
    enabled: Boolean(item && dataset === "instruments" && id),
  })
  const observationsQuery = useQuery({
    queryKey: ["lookup-detail", "observations", id],
    queryFn: ({ signal }) => loadMacroObservations(id, signal),
    enabled: Boolean(item && dataset === "macro" && id),
  })

  useEffect(() => {
    if (!item) return
    priorFocusRef.current =
      document.activeElement instanceof HTMLElement
        ? document.activeElement
        : null
    window.requestAnimationFrame(() => drawerRef.current?.focus())
    return () => priorFocusRef.current?.focus()
  }, [item])

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
                data={observationsQuery.data ?? []}
                error={observationsQuery.error}
                isLoading={observationsQuery.isPending}
                isRefreshing={
                  observationsQuery.isFetching && !observationsQuery.isPending
                }
                retry={() => void observationsQuery.refetch()}
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
                <PriceFeed
                  data={pricesQuery.data ?? []}
                  error={pricesQuery.error}
                  isLoading={pricesQuery.isPending}
                  isRefreshing={
                    pricesQuery.isFetching && !pricesQuery.isPending
                  }
                  retry={() => void pricesQuery.refetch()}
                />
              </section>
              <section aria-labelledby="lookup-actions-heading">
                <h3
                  id="lookup-actions-heading"
                  className="mb-3 text-sm font-semibold"
                >
                  近期公司事件
                </h3>
                <ActionFeed
                  data={actionsQuery.data ?? []}
                  error={actionsQuery.error}
                  isLoading={actionsQuery.isPending}
                  isRefreshing={
                    actionsQuery.isFetching && !actionsQuery.isPending
                  }
                  retry={() => void actionsQuery.refetch()}
                />
              </section>
            </>
          )}
        </div>
      </aside>
    </>
  )
}
