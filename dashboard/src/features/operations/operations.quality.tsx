import type {
  ColumnDef,
  ExpandedState,
  PaginationState,
} from "@tanstack/react-table"
import { ShieldCheck } from "lucide-react"
import { useEffect, useMemo, useState, type FormEvent } from "react"

import { DataTable } from "../../components/data-table"
import { Badge } from "../../components/ui/badge"
import { Button } from "../../components/ui/button"
import { Label } from "../../components/ui/label"
import type { DQIssue } from "../../lib/admin-api"
import {
  formatDate,
  OperationsDashboardStatus,
  PageIntro,
  Panel,
  RefreshStatus,
} from "./operations.shared"
import {
  qualitySearchSchema,
  type OperationsPageSearch,
} from "./operations.search"
import {
  useOperationsDashboardQuery,
  useOperationsDashboardState,
} from "./operations.queries"

const POLICY_VIOLATION_FIELDS = [
  "code",
  "action",
  "reason",
  "observed",
  "expected",
] as const

export function boundedPolicyValue(value: unknown, maxLength = 180) {
  if (value === null || value === undefined) return "—"
  let text: string
  if (typeof value === "string") {
    text = value
  } else if (
    typeof value === "number" ||
    typeof value === "boolean" ||
    typeof value === "bigint"
  ) {
    text = String(value)
  } else {
    try {
      text = JSON.stringify(value)
    } catch {
      text = "[無法顯示]"
    }
  }
  return text.length > maxLength ? `${text.slice(0, maxLength)}…` : text
}

function boundedPolicyField(value: unknown) {
  return typeof value === "object" && value !== null
    ? "[bounded object]"
    : boundedPolicyValue(value)
}

function boundedPolicyViolation(value: unknown) {
  if (value === null || value === undefined) return "—"
  if (typeof value !== "object" || Array.isArray(value)) {
    return boundedPolicyValue(value)
  }
  const record = value as Record<string, unknown>
  const fields = POLICY_VIOLATION_FIELDS.filter(field => field in record)
  if (fields.length === 0) return "[violation detail]"
  return fields
    .map(field => `${field}=${boundedPolicyField(record[field])}`)
    .join(" · ")
}

export function DQPolicyDetails({
  detail,
}: {
  detail: DQIssue["policy_detail"]
}) {
  if (!detail) return <span>—</span>
  const violations = Array.isArray(detail.violations) ? detail.violations : []
  const violationCount =
    typeof detail.violation_count === "number"
      ? detail.violation_count
      : typeof detail.count === "number"
        ? detail.count
        : violations.length
  const summaryEntries = POLICY_VIOLATION_FIELDS.filter(key => key in detail)
  const visibleViolations = violations.slice(0, 3)
  const truncated = detail.truncated === true || violations.length > 3

  return (
    <div className="grid max-w-xl gap-1 text-xs">
      <span className="font-semibold">
        {violationCount > 0
          ? `${violationCount} 個 policy violation`
          : "Policy detail"}
        {truncated ? "（truncated／已截斷）" : ""}
      </span>
      {summaryEntries.map(key => (
        <span className="text-muted" key={key}>
          {key}：{boundedPolicyField(detail[key])}
        </span>
      ))}
      {visibleViolations.map((violation, index) => (
        <span
          className="text-muted wrap-anywhere"
          key={`${index}:${boundedPolicyValue(violation, 40)}`}
        >
          violation {index + 1}：{boundedPolicyViolation(violation)}
        </span>
      ))}
      {truncated && visibleViolations.length === 0 && (
        <span className="text-muted">其餘細節未顯示。</span>
      )}
    </div>
  )
}

function DQProvenanceDetail({ issue }: { issue: DQIssue }) {
  return (
    <div className="grid gap-3 text-xs">
      <div className="grid gap-1 sm:grid-cols-2">
        <span>
          Provider：{issue.provider ?? "—"}
          {issue.source ? ` · Source：${issue.source}` : ""}
        </span>
        <span className="font-mono wrap-anywhere">
          Dataset：{issue.dataset_key ?? "—"}
        </span>
        <span className="font-mono wrap-anywhere">
          Run：{issue.run_id ?? "—"}
        </span>
        <span>
          Schema：{issue.schema_id ?? "—"}
          {issue.schema_version ? `.v${issue.schema_version}` : ""}
        </span>
        <span>交易日：{issue.trade_date ?? "—"}</span>
        <span>批次資料日：{issue.batch_data_date ?? "—"}</span>
        <span>Fetched：{formatDate(issue.fetched_at)}</span>
        <span className="font-mono wrap-anywhere">
          Request：{issue.request_key ?? "—"}
        </span>
        <span>
          Raw：{issue.raw_available ? "可取得" : "不可用"}
          {issue.raw_payload_id
            ? ` · Raw payload ID：${boundedPolicyValue(issue.raw_payload_id, 64)}`
            : ""}
        </span>
      </div>
      <div>
        <span className="mb-1 block font-semibold">Policy detail</span>
        <DQPolicyDetails detail={issue.policy_detail} />
      </div>
      <p className="m-0 whitespace-pre-wrap wrap-anywhere">
        {issue.description ?? "—"}
      </p>
    </div>
  )
}

const columns: ColumnDef<DQIssue, unknown>[] = [
  {
    accessorKey: "severity",
    header: "嚴重度",
    meta: { width: 100, pin: "left" },
    cell: context => {
      const severity = context.getValue<string>()
      return (
        <Badge variant={severity === "error" ? "destructive" : "warning"}>
          {severity}
        </Badge>
      )
    },
  },
  {
    accessorKey: "issue_type",
    header: "類型",
    meta: { minWidth: 150 },
    cell: context => (
      <span className="font-mono wrap-anywhere">
        {context.getValue<string>()}
      </span>
    ),
  },
  {
    id: "summary",
    header: "來源 / Dataset / 日期",
    meta: { minWidth: 250, wrap: true },
    accessorFn: issue =>
      `${issue.provider ?? "—"} · ${issue.dataset_key ?? "—"} · ${issue.trade_date ?? "—"}`,
    cell: context => (
      <span className="whitespace-normal wrap-anywhere">
        {context.getValue<string>()}
      </span>
    ),
  },
  {
    id: "raw",
    header: "Raw",
    meta: { minWidth: 90 },
    accessorFn: issue => (issue.raw_available ? "可取得" : "不可用"),
  },
  {
    accessorKey: "created_at",
    header: "建立時間",
    meta: { minWidth: 180 },
    cell: context => formatDate(context.getValue<string>()),
  },
]

export function QualityPage({
  search = qualitySearchSchema.parse({}),
  updateSearch = () => undefined,
}: {
  search?: OperationsPageSearch
  updateSearch?: (next: OperationsPageSearch) => void
} = {}) {
  const audit = {
    datasetKey: "",
    runId: "",
    dateFrom: "",
    dateTo: "",
    page: search.p,
    pageSize: search.ps,
  }
  const query = useOperationsDashboardQuery("quality", audit)
  const state = useOperationsDashboardState(query)
  const issuesResult =
    state.response?.view === "quality" ? state.response.issues : null
  const issues = issuesResult?.ok ? issuesResult.data : null
  const [pageSizeDraft, setPageSizeDraft] = useState(search.ps)
  const [expanded, setExpanded] = useState<ExpandedState>({})
  const tableColumns = useMemo(() => columns, [])

  useEffect(() => setPageSizeDraft(search.ps), [search.ps])
  useEffect(() => setExpanded({}), [search.p, search.ps])
  useEffect(() => {
    if (!issues) return
    const totalPages = Math.max(issues.pagination.total_pages, 1)
    const page = Math.min(Math.max(search.p, 1), totalPages)
    if (page !== search.p) updateSearch({ ...search, p: page })
  }, [issues, search, updateSearch])

  function submitIssues(event: FormEvent) {
    event.preventDefault()
    const parsed = qualitySearchSchema.parse({ p: 1, ps: pageSizeDraft })
    updateSearch({ ...search, p: parsed.p, ps: parsed.ps })
  }

  const pagination: PaginationState = {
    pageIndex: Math.max(search.p - 1, 0),
    pageSize: search.ps,
  }

  return (
    <>
      <PageIntro
        eyebrow="Correctness"
        title="資料品質"
        description="集中檢視尚未解決的 DQ 規則問題。"
      />
      <OperationsDashboardStatus state={state} />
      <Panel
        eyebrow="Unresolved issues"
        title="未解決 DQ 問題"
        icon={<ShieldCheck size={19} />}
        result={issuesResult}
        loading={state.initialLoading}
      >
        {issues ? (
          <>
            <form
              className="mb-4 flex flex-wrap items-end justify-end gap-2.5"
              onSubmit={submitIssues}
            >
              <div className="grid w-full gap-1.5 sm:w-36">
                <Label htmlFor="quality-page-size">每頁筆數</Label>
                <select
                  id="quality-page-size"
                  className="h-9 w-full rounded-lg border border-line bg-surface px-3 text-sm text-ink outline-none focus-visible:ring-3 focus-visible:ring-accent/20"
                  value={pageSizeDraft}
                  onChange={event =>
                    setPageSizeDraft(Number(event.target.value))
                  }
                  disabled={state.pending}
                >
                  <option value={25}>25</option>
                  <option value={50}>50</option>
                  <option value={100}>100</option>
                </select>
              </div>
              <Button type="submit" disabled={state.pending}>
                查詢
              </Button>
            </form>
            <RefreshStatus
              pending={state.pending}
              label="正在更新資料品質…目前資料仍可使用。"
            />
            <DataTable
              ariaLabel="未解決 DQ 問題"
              caption="未解決 DQ 問題"
              columns={tableColumns}
              data={issues.data}
              emptyState="目前沒有未解決的資料品質問題。"
              expanded={expanded}
              getRowCanExpand={() => true}
              getRowId={row => row.id}
              isRefreshing={state.pending && !state.initialLoading}
              manualPagination
              pageCount={Math.max(issues.pagination.total_pages, 1)}
              pagination={pagination}
              pageSizeOptions={[25, 50, 100]}
              renderExpandedRow={row => (
                <DQProvenanceDetail issue={row.original} />
              )}
              rowCount={issues.pagination.total_records}
              onExpandedChange={setExpanded}
              onPaginationChange={next => {
                const nextState =
                  typeof next === "function" ? next(pagination) : next
                if (nextState.pageSize !== search.ps) {
                  updateSearch({
                    ...search,
                    p: 1,
                    ps: nextState.pageSize as OperationsPageSearch["ps"],
                  })
                  return
                }
                if (nextState.pageIndex !== pagination.pageIndex) {
                  updateSearch({ ...search, p: nextState.pageIndex + 1 })
                }
              }}
            />
          </>
        ) : null}
      </Panel>
    </>
  )
}
