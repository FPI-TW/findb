import { useQuery } from "@tanstack/react-query"
import {
  ChevronDown,
  Clipboard,
  Database,
  Download,
  RefreshCw,
  Search,
  SlidersHorizontal,
} from "lucide-react"
import { useEffect, useMemo, useRef, useState } from "react"
import {
  functionalUpdate,
  type ColumnDef,
  type OnChangeFn,
  type PaginationState,
  type SortingState,
} from "@tanstack/react-table"

import { DataTable } from "../../components/data-table"
import { Alert, AlertDescription, AlertTitle } from "../../components/ui/alert"
import { Badge } from "../../components/ui/badge"
import { Button } from "../../components/ui/button"
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "../../components/ui/card"
import { Input } from "../../components/ui/input"
import { Label } from "../../components/ui/label"
import { Skeleton } from "../../components/ui/skeleton"
import { DATASET_CONFIG, LOOKUP_STORAGE_KEY, PAGE_SIZES } from "./config"
import { loadAllLookupItems, loadLookupPage } from "./data"
import { DetailDrawer } from "./DetailDrawer"
import type {
  DatasetKey,
  LookupItem,
  LookupResponse,
  LookupSearch,
} from "./types"
import { buildCsv, formatCell, nextDatasetSearch } from "./utils"

interface PersistedPreferences {
  instruments?: Partial<LookupSearch>
  macro?: Partial<LookupSearch>
}

function readPreferences(): PersistedPreferences {
  try {
    const stored = window.localStorage.getItem(LOOKUP_STORAGE_KEY)
    return stored ? (JSON.parse(stored) as PersistedPreferences) : {}
  } catch {
    return {}
  }
}

function savePreferences(search: LookupSearch) {
  try {
    const preferences = readPreferences()
    preferences[search.ds] = {
      m: search.m,
      ac: search.ac,
      st: search.st,
      fq: search.fq,
      src: search.src,
      sb: search.sb,
      sd: search.sd,
      ps: search.ps,
    }
    window.localStorage.setItem(LOOKUP_STORAGE_KEY, JSON.stringify(preferences))
  } catch {
    // Preferences are optional when storage is disabled.
  }
}

function LookupLoading() {
  return (
    <Card role="status" aria-live="polite">
      <span className="sr-only">正在載入金融商品與宏觀序列</span>
      <CardHeader>
        <Skeleton className="h-5 w-40" />
        <Skeleton className="h-4 w-72 max-w-full" />
      </CardHeader>
      <CardContent className="space-y-3">
        <Skeleton className="h-10 w-full" />
        {Array.from({ length: 6 }, (_, index) => (
          <Skeleton key={index} className="h-9 w-full" />
        ))}
      </CardContent>
    </Card>
  )
}

function SelectField({
  id,
  label,
  value,
  options,
  onChange,
}: {
  id: string
  label: string
  value: string
  options: string[]
  onChange: (value: string) => void
}) {
  return (
    <div className="space-y-1.5">
      <Label htmlFor={id}>{label}</Label>
      <div className="relative">
        <select
          id={id}
          className="h-9 w-full appearance-none rounded-lg border border-line bg-surface px-3 pr-8 text-sm outline-none transition focus:border-accent focus:ring-2 focus:ring-accent/20"
          value={value}
          onChange={event => onChange(event.target.value)}
        >
          <option value="ALL">全部</option>
          {options.map(option => (
            <option key={option} value={option}>
              {option}
            </option>
          ))}
        </select>
        <ChevronDown
          className="pointer-events-none absolute top-2.5 right-2.5 text-muted"
          size={15}
          aria-hidden
        />
      </div>
    </div>
  )
}

export function LookupPage({
  search,
  updateSearch,
}: {
  search: LookupSearch
  updateSearch: (next: LookupSearch) => void
}) {
  const [exporting, setExporting] = useState(false)
  const [queryInput, setQueryInput] = useState(search.q)
  const [announcement, setAnnouncement] = useState("")
  const restoredPreferences = useRef(false)

  const lookupQuery = useQuery({
    queryKey: ["lookup", search],
    queryFn: ({ signal }) => loadLookupPage(search, signal),
  })

  useEffect(() => {
    if (restoredPreferences.current) return
    restoredPreferences.current = true
    if (window.location.search !== "") return
    const stored = readPreferences()[search.ds]
    if (stored) updateSearch(nextDatasetSearch(search.ds, stored))
  }, [search.ds, updateSearch])

  useEffect(() => {
    setQueryInput(search.q)
  }, [search.q])

  useEffect(() => {
    const timer = window.setTimeout(() => {
      if (queryInput !== search.q) {
        updateSearch({ ...search, q: queryInput, p: 1, id: "" })
      }
    }, 200)
    return () => window.clearTimeout(timer)
  }, [queryInput, search, updateSearch])

  useEffect(() => {
    savePreferences(search)
  }, [search])

  useEffect(() => {
    if (!announcement) return
    const timer = window.setTimeout(() => setAnnouncement(""), 1800)
    return () => window.clearTimeout(timer)
  }, [announcement])

  useEffect(() => {
    if (!lookupQuery.data) return
    const lastPage = Math.max(lookupQuery.data.pagination.total_pages, 1)
    if (search.p > lastPage) {
      updateSearch({ ...search, p: lastPage, id: "" })
    }
  }, [lookupQuery.data, search, updateSearch])

  const config = DATASET_CONFIG[search.ds]
  const response = lookupQuery.data as LookupResponse | undefined
  const items = (response?.data ?? []) as LookupItem[]
  const totalPages = Math.max(1, response?.pagination.total_pages ?? 1)
  const currentPage = Math.min(search.p, totalPages)
  const selectedItem =
    items.find(item => {
      const record = item as unknown as Record<string, unknown>
      return (
        String(
          search.ds === "macro" ? record.series_id : record.instrument_id
        ) === search.id
      )
    }) ?? null

  const currentSorting: SortingState = [
    { id: search.sb, desc: search.sd === "desc" },
  ]

  const tableColumns = useMemo<ColumnDef<LookupItem, unknown>[]>(() => {
    const dataColumns = config.columns.map(column => ({
      id: column.key,
      accessorFn: (item: LookupItem) =>
        (item as unknown as Record<string, unknown>)[column.key],
      header: column.label,
      enableSorting: true,
      meta: {
        minWidth:
          column.key === "name" || column.key === "source"
            ? 160
            : column.key === "symbol" || column.key === "source_code"
              ? 132
              : 104,
      },
      cell: ({ row }: { row: { original: LookupItem } }) => {
        const copyColumn =
          (search.ds === "macro" && column.key === "source_code") ||
          (search.ds === "instruments" && column.key === "symbol")
        const record = row.original as unknown as Record<string, unknown>
        const copyValue =
          search.ds === "macro" ? record.source_code : record.symbol
        const value = formatCell(row.original, column)
        if (copyColumn) {
          return (
            <button
              type="button"
              className={
                column.key === "symbol"
                  ? "inline-flex items-center gap-1 font-sans font-bold tracking-wide text-accent hover:underline"
                  : "inline-flex items-center gap-1 font-mono font-semibold text-accent hover:underline"
              }
              aria-label={`複製 ${String(copyValue ?? "")}`}
              title={`複製 ${String(copyValue ?? "")}`}
              onClick={() =>
                void copyIdentifier(
                  copyValue,
                  search.ds === "macro" ? "Source Code" : "Symbol"
                )
              }
            >
              {value}
              <Clipboard size={13} aria-hidden />
            </button>
          )
        }
        return column.type === "status" ? (
          <Badge variant={value === "active" ? "secondary" : "outline"}>
            {value}
          </Badge>
        ) : (
          value
        )
      },
    }))

    return [
      ...dataColumns,
      {
        id: "detail",
        header: "詳細",
        enableSorting: false,
        meta: { width: 76, pin: "right", align: "center" },
        cell: ({ row }: { row: { original: LookupItem } }) => {
          const record = row.original as unknown as Record<string, unknown>
          const id = String(
            search.ds === "macro" ? record.series_id : record.instrument_id
          )
          const title = String(
            search.ds === "macro"
              ? record.source_code || record.name || id
              : record.symbol || id
          )
          return (
            <Button
              type="button"
              size="icon-xs"
              variant="ghost"
              aria-label={`查看 ${title} 詳情`}
              title={`查看 ${title} 詳情`}
              onClick={() => patchSearch({ id })}
            >
              <Search aria-hidden="true" />
            </Button>
          )
        },
      },
    ]
  }, [config.columns, search])

  const handleSortingChange: OnChangeFn<SortingState> = nextOrUpdater => {
    const next = functionalUpdate(nextOrUpdater, currentSorting)
    const first = next[0] ?? { id: search.sb, desc: false }
    if (!config.columns.some(column => column.key === first.id)) return
    updateSearch({
      ...search,
      sb: first.id as LookupSearch["sb"],
      sd: first.desc ? "desc" : "asc",
      p: 1,
      id: "",
    })
  }

  const handlePaginationChange: OnChangeFn<PaginationState> = nextOrUpdater => {
    const currentPagination = {
      pageIndex: currentPage - 1,
      pageSize: search.ps,
    }
    const next = functionalUpdate(nextOrUpdater, currentPagination)
    const pageSize = PAGE_SIZES.includes(
      next.pageSize as (typeof PAGE_SIZES)[number]
    )
      ? next.pageSize
      : search.ps
    updateSearch({
      ...search,
      p: next.pageSize === search.ps ? next.pageIndex + 1 : 1,
      ps: pageSize,
      id: "",
    })
  }

  function patchSearch(patch: Partial<LookupSearch>) {
    updateSearch({ ...search, ...patch })
  }

  function switchDataset(dataset: DatasetKey) {
    const persisted = readPreferences()[dataset]
    updateSearch(nextDatasetSearch(dataset, persisted))
  }

  function clearFilters() {
    updateSearch(
      nextDatasetSearch(search.ds, {
        ps: search.ps,
        st: search.ds === "instruments" ? "active" : "ALL",
      })
    )
    setQueryInput("")
  }

  async function exportCsv() {
    setExporting(true)
    try {
      const allItems = await loadAllLookupItems(search)
      const csv = buildCsv(allItems, config.columns)
      const url = URL.createObjectURL(
        new Blob([csv], { type: "text/csv;charset=utf-8" })
      )
      const anchor = document.createElement("a")
      anchor.href = url
      anchor.download = `findb-${search.ds}-${new Date()
        .toISOString()
        .slice(0, 10)
        .replaceAll("-", "")}.csv`
      anchor.click()
      URL.revokeObjectURL(url)
      setAnnouncement(`已匯出 ${allItems.length.toLocaleString()} 筆資料`)
    } catch (reason) {
      setAnnouncement(reason instanceof Error ? reason.message : "無法匯出 CSV")
    } finally {
      setExporting(false)
    }
  }

  async function copyIdentifier(value: unknown, label: string) {
    try {
      await navigator.clipboard.writeText(String(value ?? ""))
      setAnnouncement(`${label}已複製`)
    } catch {
      setAnnouncement("無法存取剪貼簿")
    }
  }

  const facets = response?.facets
  const markets = facets?.markets ?? []
  const secondaryOptions =
    search.ds === "macro" && facets && "frequencies" in facets
      ? facets.frequencies
      : facets && "asset_classes" in facets
        ? facets.asset_classes
        : []
  const tertiaryOptions =
    search.ds === "macro" && facets && "sources" in facets
      ? facets.sources
      : facets && "statuses" in facets
        ? facets.statuses
        : []
  const totalRecords = response?.pagination.total_records ?? 0
  const filtersDirty =
    search.q !== "" ||
    search.m !== "ALL" ||
    (search.ds === "macro"
      ? search.fq !== "ALL" || search.src !== "ALL"
      : search.ac !== "ALL" || search.st !== "active")

  const queryError = lookupQuery.error
  const hasResponse = response !== undefined
  const initialLoading = lookupQuery.isPending && !hasResponse
  const refreshing = lookupQuery.isFetching && hasResponse
  const errorMessage =
    queryError instanceof Error ? queryError.message : "無法載入查詢資料。"

  return (
    <main className="mx-auto w-full max-w-screen-2xl flex-1 space-y-5 px-4 py-8 sm:px-6 lg:px-8">
      <header className="flex flex-col justify-between gap-4 lg:flex-row lg:items-end">
        <div>
          <p className="font-mono text-xs font-medium tracking-widest text-accent uppercase">
            Public data explorer
          </p>
          <h1 className="mt-2 text-3xl font-semibold tracking-tight">
            標的與宏觀查詢
          </h1>
          <p className="mt-2 max-w-2xl text-sm leading-6 text-muted">
            查詢 FinDB 金融商品與宏觀序列，並查看最近價格、公司事件與觀測值。
          </p>
        </div>
        {response && (
          <div className="text-sm text-muted">
            目前條件共 {totalRecords.toLocaleString()} 筆
          </div>
        )}
      </header>

      <Card className="gap-4 py-4">
        <CardHeader className="px-4 sm:px-5">
          <div
            className="grid grid-cols-2 gap-2"
            role="tablist"
            aria-label="資料集"
          >
            {(["instruments", "macro"] as const).map(dataset => {
              const active = search.ds === dataset
              const count =
                active && response
                  ? response.pagination.total_records
                  : undefined
              return (
                <Button
                  key={dataset}
                  type="button"
                  role="tab"
                  aria-selected={active}
                  variant={active ? "default" : "outline"}
                  onClick={() => switchDataset(dataset)}
                >
                  {DATASET_CONFIG[dataset].label}
                  <Badge variant={active ? "secondary" : "outline"}>
                    {count?.toLocaleString() ?? "—"}
                  </Badge>
                </Button>
              )
            })}
          </div>
        </CardHeader>
        <CardContent className="space-y-4 px-4 sm:px-5">
          <div className="relative">
            <Search
              className="pointer-events-none absolute top-2.5 left-3 text-muted"
              size={18}
              aria-hidden
            />
            <Input
              className="pl-10"
              type="search"
              value={queryInput}
              placeholder={config.searchPlaceholder}
              aria-label="搜尋"
              onChange={event => setQueryInput(event.target.value)}
            />
          </div>
          <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
            <SelectField
              id="lookup-market"
              label="市場"
              value={search.m}
              options={markets}
              onChange={m => patchSearch({ m, p: 1, id: "" })}
            />
            <SelectField
              id="lookup-secondary"
              label={search.ds === "macro" ? "頻率" : "類別"}
              value={search.ds === "macro" ? search.fq : search.ac}
              options={secondaryOptions}
              onChange={value =>
                patchSearch(
                  search.ds === "macro"
                    ? { fq: value, p: 1, id: "" }
                    : { ac: value, p: 1, id: "" }
                )
              }
            />
            <SelectField
              id="lookup-tertiary"
              label={search.ds === "macro" ? "來源" : "狀態"}
              value={search.ds === "macro" ? search.src : search.st}
              options={tertiaryOptions}
              onChange={value =>
                patchSearch(
                  search.ds === "macro"
                    ? { src: value, p: 1, id: "" }
                    : { st: value, p: 1, id: "" }
                )
              }
            />
            <div className="flex items-end">
              <Button
                className="w-full"
                type="button"
                variant="outline"
                disabled={!filtersDirty}
                onClick={clearFilters}
              >
                <SlidersHorizontal />
                清除條件
              </Button>
            </div>
          </div>
        </CardContent>
      </Card>

      {queryError && hasResponse && (
        <Alert variant="destructive">
          <Database />
          <AlertTitle>更新查詢結果失敗</AlertTitle>
          <AlertDescription className="flex flex-wrap items-center justify-between gap-3">
            <span>{errorMessage}</span>
            <Button
              type="button"
              variant="outline"
              onClick={() => void lookupQuery.refetch()}
            >
              <RefreshCw />
              重新載入
            </Button>
          </AlertDescription>
        </Alert>
      )}

      {initialLoading ? (
        <LookupLoading />
      ) : queryError && !hasResponse ? (
        <Alert variant="destructive">
          <Database />
          <AlertTitle>無法載入查詢資料</AlertTitle>
          <AlertDescription className="space-y-3">
            <p>{errorMessage}</p>
            <Button
              type="button"
              variant="outline"
              onClick={() => void lookupQuery.refetch()}
            >
              <RefreshCw />
              重新載入
            </Button>
          </AlertDescription>
        </Alert>
      ) : (
        <Card className="gap-0 py-0">
          <CardHeader className="border-b border-line px-4 py-4 sm:px-5">
            <div className="flex flex-col justify-between gap-3 sm:flex-row sm:items-center">
              <div>
                <CardTitle>
                  {totalRecords.toLocaleString()} 筆符合條件
                </CardTitle>
                <CardDescription className="mt-1">
                  顯示{" "}
                  {totalRecords === 0 ? 0 : (currentPage - 1) * search.ps + 1}–
                  {Math.min(currentPage * search.ps, totalRecords)} /{" "}
                  {totalRecords.toLocaleString()}
                </CardDescription>
              </div>
              <div className="flex flex-wrap items-center gap-2">
                {refreshing && (
                  <Badge variant="outline" aria-live="polite">
                    更新中
                  </Badge>
                )}
                <Button
                  type="button"
                  variant="outline"
                  disabled={totalRecords === 0 || exporting}
                  onClick={() => void exportCsv()}
                >
                  <Download />
                  {exporting ? "匯出中…" : "匯出 CSV"}
                </Button>
              </div>
            </div>
          </CardHeader>
          <CardContent className="px-0 pb-4">
            <DataTable
              ariaLabel={`${config.label}查詢結果`}
              caption={`${config.label}查詢結果`}
              columns={tableColumns}
              data={items}
              emptyState={
                <div className="grid min-h-64 place-items-center px-5 text-center">
                  <div>
                    <Database className="mx-auto text-muted" aria-hidden />
                    <p className="mt-3 font-medium">
                      沒有符合條件的{config.itemLabel}
                    </p>
                    {filtersDirty && (
                      <Button
                        className="mt-3"
                        type="button"
                        variant="outline"
                        onClick={clearFilters}
                      >
                        清除條件
                      </Button>
                    )}
                  </div>
                </div>
              }
              getRowId={item => {
                const record = item as unknown as Record<string, unknown>
                return String(
                  search.ds === "macro"
                    ? record.series_id
                    : record.instrument_id
                )
              }}
              isRefreshing={refreshing}
              manualPagination
              manualSorting
              onPaginationChange={handlePaginationChange}
              onSortingChange={handleSortingChange}
              pageCount={totalPages}
              pageSizeOptions={[...PAGE_SIZES]}
              pagination={{ pageIndex: currentPage - 1, pageSize: search.ps }}
              rowCount={totalRecords}
              sorting={currentSorting}
              tableClassName="min-w-[900px]"
            />
          </CardContent>
        </Card>
      )}

      <div
        className="pointer-events-none fixed right-4 bottom-4 z-60 min-h-10 rounded-lg bg-ink px-4 py-2.5 text-sm text-surface shadow-lg empty:hidden"
        role="status"
        aria-live="polite"
      >
        {announcement}
      </div>

      <DetailDrawer
        key={search.id || "closed"}
        dataset={search.ds}
        item={selectedItem}
        onClose={() => patchSearch({ id: "" })}
        onCopied={setAnnouncement}
      />
    </main>
  )
}
