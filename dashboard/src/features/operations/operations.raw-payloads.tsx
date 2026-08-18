import type {
  ColumnDef,
  ExpandedState,
  PaginationState,
} from "@tanstack/react-table"
import { Search, TriangleAlert } from "lucide-react"
import { useEffect, useMemo, useState, type FormEvent } from "react"

import { DataTable } from "../../components/data-table"
import { Alert, AlertDescription } from "../../components/ui/alert"
import { Button } from "../../components/ui/button"
import {
  Card,
  CardContent,
  CardHeader,
  CardTitle,
} from "../../components/ui/card"
import { Input } from "../../components/ui/input"
import { Label } from "../../components/ui/label"
import type { RawPayload, RawPayloadListItem } from "../../lib/admin-api"
import { isDashboardAuthenticationError } from "../../lib/auth-errors"
import {
  formatDate,
  OperationsDashboardStatus,
  PageIntro,
  RefreshStatus,
} from "./operations.shared"
import {
  rawPayloadAuditFromSearch,
  rawPayloadsSearchSchema,
  type RawPayloadsSearch,
} from "./operations.search"
import {
  useOperationsDashboardQuery,
  useOperationsDashboardState,
  useRawPayloadDetailQuery,
} from "./operations.queries"

const columns: ColumnDef<RawPayloadListItem, unknown>[] = [
  {
    accessorKey: "created_at",
    header: "建立時間",
    meta: { minWidth: 190, pin: "left" },
    cell: context => formatDate(context.getValue<string>()),
  },
  {
    accessorKey: "dataset_key",
    header: "Dataset",
    meta: { minWidth: 180 },
    cell: context => (
      <span className="font-mono wrap-anywhere">
        {context.getValue<string>()}
      </span>
    ),
  },
  {
    accessorKey: "source",
    header: "來源",
    meta: { minWidth: 120 },
  },
  {
    accessorKey: "run_id",
    header: "Run ID",
    meta: { minWidth: 260 },
    cell: context => (
      <span className="font-mono wrap-anywhere">
        {context.getValue<string>()}
      </span>
    ),
  },
  {
    accessorKey: "expire_at",
    header: "保留期限",
    meta: { minWidth: 190 },
    cell: context => formatDate(context.getValue<string>()),
  },
  {
    id: "schema",
    header: "Schema",
    meta: { minWidth: 130 },
    accessorFn: payload =>
      `${payload.schema_id ?? "—"}${payload.schema_version ? `.v${payload.schema_version}` : ""}`,
  },
]

function rawDetailError(error: unknown) {
  if (isDashboardAuthenticationError(error))
    return "Dashboard authentication required"
  if (error instanceof Error && error.message.includes("(404)")) {
    return "原始資料已不存在"
  }
  return error instanceof Error && error.message
    ? error.message
    : "無法取得原始資料"
}

function RawPayloadDetailRow({
  scope,
  rawPayloadId,
}: {
  scope: RawPayloadsSearch
  rawPayloadId: string
}) {
  const query = useRawPayloadDetailQuery(scope, rawPayloadId, true)
  if (query.isPending) {
    return <span role="status">正在載入完整 JSON…</span>
  }
  if (query.error) {
    return (
      <Alert variant="destructive">
        <TriangleAlert />
        <AlertDescription>{rawDetailError(query.error)}</AlertDescription>
      </Alert>
    )
  }
  const payload: RawPayload | undefined = query.data
  if (!payload) return <span>原始資料已不存在</span>
  return (
    <pre className="m-0 w-full rounded-lg border border-line bg-surface p-4 font-mono text-xs leading-relaxed whitespace-pre-wrap text-ink wrap-anywhere">
      {JSON.stringify(payload.payload, null, 2)}
    </pre>
  )
}

export function RawPayloadsPage({
  search = rawPayloadsSearchSchema.parse({}),
  updateSearch = () => undefined,
}: {
  search?: RawPayloadsSearch
  updateSearch?: (next: RawPayloadsSearch) => void
} = {}) {
  const audit = rawPayloadAuditFromSearch(search)
  const query = useOperationsDashboardQuery("rawPayloads", audit)
  const state = useOperationsDashboardState(query)
  const rawResult =
    state.response?.view === "rawPayloads" ? state.response.rawPayloads : null
  const rawPayloads = rawResult?.ok ? rawResult.data : null
  const [draft, setDraft] = useState(search)
  const [expanded, setExpanded] = useState<ExpandedState>({})
  const tableColumns = useMemo(() => columns, [])

  useEffect(() => setDraft(search), [search])
  useEffect(
    () => setExpanded({}),
    [search.dataset, search.run, search.from, search.to, search.p, search.ps]
  )
  useEffect(() => {
    if (!rawPayloads) return
    const totalPages = Math.max(rawPayloads.pagination.total_pages, 1)
    const page = Math.min(Math.max(search.p, 1), totalPages)
    if (page !== search.p) updateSearch({ ...search, p: page })
  }, [rawPayloads, search, updateSearch])

  function submitAudit(event: FormEvent) {
    event.preventDefault()
    const parsed = rawPayloadsSearchSchema.parse({
      ...draft,
      p: 1,
    })
    updateSearch(parsed)
  }

  const pagination: PaginationState = {
    pageIndex: Math.max(search.p - 1, 0),
    pageSize: search.ps,
  }

  return (
    <>
      <PageIntro
        eyebrow="Raw payload retrieval"
        title="原始資料稽核"
        description="依資料集、run 與日期範圍查詢保留中的 raw payload。"
      />
      <OperationsDashboardStatus state={state} />
      <Card className="gap-0 p-5">
        <CardHeader className="mb-4 flex grid-cols-none flex-row items-center gap-3 px-0">
          <span className="inline-flex size-9 shrink-0 items-center justify-center rounded-xl bg-accent-soft text-accent">
            <Search size={19} />
          </span>
          <CardTitle asChild className="text-base tracking-tight">
            <h2>原始資料查詢</h2>
          </CardTitle>
        </CardHeader>
        <CardContent className="px-0">
          <form
            className="mb-4 grid grid-cols-2 items-end gap-2.5 xl:grid-cols-7"
            onSubmit={submitAudit}
          >
            <div className="grid gap-1.5 xl:col-span-2">
              <Label htmlFor="dataset-key">Dataset key</Label>
              <Input
                id="dataset-key"
                value={draft.dataset}
                onChange={event =>
                  setDraft({ ...draft, dataset: event.target.value })
                }
              />
            </div>
            <div className="grid gap-1.5">
              <Label htmlFor="run-id">Run ID</Label>
              <Input
                id="run-id"
                value={draft.run}
                onChange={event =>
                  setDraft({ ...draft, run: event.target.value })
                }
              />
            </div>
            <div className="grid gap-1.5">
              <Label htmlFor="date-from">起始日期</Label>
              <Input
                id="date-from"
                type="date"
                value={draft.from}
                onChange={event =>
                  setDraft({ ...draft, from: event.target.value })
                }
              />
            </div>
            <div className="grid gap-1.5">
              <Label htmlFor="date-to">結束日期</Label>
              <Input
                id="date-to"
                type="date"
                value={draft.to}
                onChange={event =>
                  setDraft({ ...draft, to: event.target.value })
                }
              />
            </div>
            <div className="grid gap-1.5">
              <Label htmlFor="page-size">每頁筆數</Label>
              <select
                id="page-size"
                className="h-9 w-full rounded-lg border border-line bg-surface px-3 text-sm text-ink outline-none focus-visible:ring-3 focus-visible:ring-accent/20"
                value={draft.ps}
                onChange={event =>
                  setDraft({
                    ...draft,
                    ps: Number(event.target.value) as RawPayloadsSearch["ps"],
                  })
                }
                disabled={state.pending}
              >
                <option value={25}>25</option>
                <option value={50}>50</option>
                <option value={100}>100</option>
              </select>
            </div>
            <Button
              className="col-span-2 xl:col-span-1"
              type="submit"
              disabled={state.pending}
            >
              <Search size={16} /> 查詢
            </Button>
          </form>
          <RefreshStatus
            pending={state.pending}
            label="正在更新原始資料…目前資料仍可使用。"
          />
          {state.initialLoading ? null : !rawResult ? (
            <Alert variant="destructive">
              <TriangleAlert />
              <AlertDescription>暫時無法查詢 raw payload</AlertDescription>
            </Alert>
          ) : rawPayloads ? (
            <DataTable
              ariaLabel="原始資料稽核"
              caption="原始資料稽核"
              columns={tableColumns}
              data={rawPayloads.data}
              emptyState="查無符合條件的原始資料。"
              expanded={expanded}
              getRowCanExpand={() => true}
              getRowId={row => row.raw_payload_id}
              isRefreshing={state.pending && !state.initialLoading}
              manualPagination
              pageCount={Math.max(rawPayloads.pagination.total_pages, 1)}
              pagination={pagination}
              pageSizeOptions={[25, 50, 100]}
              renderExpandedRow={row => (
                <RawPayloadDetailRow
                  rawPayloadId={row.original.raw_payload_id}
                  scope={search}
                />
              )}
              rowCount={rawPayloads.pagination.total_records}
              onExpandedChange={setExpanded}
              onPaginationChange={next => {
                const nextState =
                  typeof next === "function" ? next(pagination) : next
                if (nextState.pageSize !== search.ps) {
                  updateSearch({
                    ...search,
                    p: 1,
                    ps: nextState.pageSize as RawPayloadsSearch["ps"],
                  })
                  return
                }
                if (nextState.pageIndex !== pagination.pageIndex) {
                  updateSearch({ ...search, p: nextState.pageIndex + 1 })
                }
              }}
            />
          ) : null}
        </CardContent>
      </Card>
      <Alert className="mt-5" variant="subtle" role="note">
        <AlertDescription>
          Raw payload 受保留政策影響，過期資料可能無法從此查詢取得。
        </AlertDescription>
      </Alert>
    </>
  )
}
