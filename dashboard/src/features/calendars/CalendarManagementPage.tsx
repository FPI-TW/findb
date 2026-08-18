import { useQuery, useQueryClient } from "@tanstack/react-query"
import { useServerFn } from "@tanstack/react-start"
import type { ColumnDef } from "@tanstack/react-table"
import {
  CalendarDays,
  FileJson2,
  FileUp,
  Pencil,
  RefreshCw,
  Send,
  Upload,
} from "lucide-react"
import {
  type ChangeEvent,
  type FormEvent,
  useEffect,
  useMemo,
  useState,
} from "react"

import { DataTable } from "../../components/data-table"
import { useProtectedQueryScope } from "../../components/ProtectedQueryScope"
import { Alert, AlertDescription, AlertTitle } from "../../components/ui/alert"
import { Badge } from "../../components/ui/badge"
import { Button } from "../../components/ui/button"
import {
  Card,
  CardContent,
  CardHeader,
  CardTitle,
} from "../../components/ui/card"
import { Input } from "../../components/ui/input"
import { Label } from "../../components/ui/label"
import { Skeleton } from "../../components/ui/skeleton"
import { toast } from "../../components/ui/toast"
import type { AdminRole } from "../../lib/admin-governance-api"
import {
  calendarDayName,
  calendarStatusLabel,
  type CalendarDay,
  type CalendarDayStatus,
  type CalendarImport,
  type CalendarPreview,
  type CalendarRevision,
  type CalendarYear,
} from "../../lib/calendar-api"
import {
  applyCalendarPreview,
  editCalendarDay,
  loadCalendarImports,
  loadCalendarMarkets,
  loadCalendarRevisions,
  loadCalendarYear,
  previewCalendarCsv,
  previewCalendarJson,
  publishCalendarYear,
  rollbackCalendarYear,
} from "../../lib/calendar.functions"
import type { CalendarSearch } from "./calendar.search"

type Tab = "year" | "manual" | "json" | "csv" | "history"
const TABS: { id: Tab; label: string }[] = [
  { id: "year", label: "全年檢視" },
  { id: "manual", label: "手動管理" },
  { id: "json", label: "JSON 匯入" },
  { id: "csv", label: "CSV 上傳" },
  { id: "history", label: "修訂／匯入紀錄" },
]

function statusVariant(status: CalendarDayStatus) {
  if (status === "open") return "default" as const
  if (status === "closed") return "secondary" as const
  return "warning" as const
}

function formatDate(value: string | null | undefined) {
  if (!value) return "—"
  return new Intl.DateTimeFormat("zh-TW", {
    dateStyle: "medium",
    timeStyle: "short",
  }).format(new Date(value))
}

function weekday(date: string) {
  return new Intl.DateTimeFormat("zh-TW", {
    weekday: "short",
    timeZone: "UTC",
  }).format(new Date(`${date}T00:00:00Z`))
}

function revisionText(year: CalendarYear) {
  const draft = year.draft_revision?.revision
  const published = year.published_revision?.revision
  if (draft !== undefined && published !== undefined)
    return `草稿 r${draft}；已發布 r${published}`
  if (draft !== undefined) return `草稿 r${draft}`
  if (published !== undefined) return `已發布 r${published}`
  return "尚未建立"
}

const calendarKeys = {
  all: ["calendar"] as const,
  markets: (scope: string) => ["calendar", scope, "markets"] as const,
  year: (scope: string, market: string, year: number) =>
    ["calendar", scope, "year", market, year] as const,
  imports: (scope: string, market: string, year: number) =>
    ["calendar", scope, "imports", market, year] as const,
  revisions: (scope: string, market: string, year: number) =>
    ["calendar", scope, "revisions", market, year] as const,
}

export function CalendarManagementPage({
  role,
  search,
  updateSearch,
}: {
  role: AdminRole
  search: CalendarSearch
  updateSearch: (next: CalendarSearch) => void
}) {
  const getMarkets = useServerFn(loadCalendarMarkets)
  const getYear = useServerFn(loadCalendarYear)
  const getImports = useServerFn(loadCalendarImports)
  const getRevisions = useServerFn(loadCalendarRevisions)
  const queryClient = useQueryClient()
  const sessionScope = useProtectedQueryScope()
  const [tab, setTab] = useState<Tab>("year")

  const marketsQuery = useQuery({
    queryKey: calendarKeys.markets(sessionScope),
    queryFn: () => getMarkets(),
    retry: false,
    refetchOnWindowFocus: false,
  })
  const markets = marketsQuery.data ?? []
  const preferredMarket =
    markets.find(item => item.market === search.market)?.market ??
    markets.find(item => item.market === "TW")?.market ??
    markets[0]?.market ??
    ""
  const market = preferredMarket
  const year = search.year

  useEffect(() => {
    if (market && market !== search.market) {
      updateSearch({ ...search, market })
    }
  }, [market, search, updateSearch])

  const selection = { market, year }
  const yearQuery = useQuery({
    queryKey: calendarKeys.year(sessionScope, market, year),
    queryFn: () => getYear({ data: selection }),
    enabled: market !== "",
    retry: false,
    refetchOnWindowFocus: false,
  })
  const importsQuery = useQuery({
    queryKey: calendarKeys.imports(sessionScope, market, year),
    queryFn: () => getImports({ data: selection }),
    enabled: market !== "",
    retry: false,
    refetchOnWindowFocus: false,
  })
  const revisionsQuery = useQuery({
    queryKey: calendarKeys.revisions(sessionScope, market, year),
    queryFn: () => getRevisions({ data: selection }),
    enabled: market !== "",
    retry: false,
    refetchOnWindowFocus: false,
  })
  const calendar = yearQuery.data ?? null
  const imports = importsQuery.data ?? []
  const revisions = revisionsQuery.data ?? []
  const initialLoading =
    marketsQuery.isPending || (market !== "" && yearQuery.isPending)
  const pending =
    marketsQuery.isFetching ||
    yearQuery.isFetching ||
    importsQuery.isFetching ||
    revisionsQuery.isFetching
  const queryError =
    marketsQuery.error ??
    yearQuery.error ??
    importsQuery.error ??
    revisionsQuery.error
  const error =
    queryError instanceof Error
      ? queryError.message
      : queryError
        ? "無法載入交易日曆。"
        : ""

  async function refresh() {
    await queryClient.invalidateQueries({
      queryKey: market ? calendarKeys.all : calendarKeys.markets(sessionScope),
    })
  }

  function selectMarket(nextMarket: string) {
    updateSearch({ ...search, market: nextMarket })
  }

  function selectYear(nextYear: number) {
    updateSearch({ ...search, year: nextYear })
  }

  const canEdit = role === "owner" || role === "operator"
  const canPublish = role === "owner"

  return (
    <div className="grid gap-5">
      <header className="flex flex-wrap items-end justify-between gap-4">
        <div>
          <p className="mb-1 font-mono text-xs font-medium tracking-widest text-accent uppercase">
            Market calendar
          </p>
          <h2 className="text-2xl font-bold tracking-tight sm:text-3xl">
            交易日曆管理
          </h2>
          <p className="mt-2 text-sm text-muted">
            各市場與年度分開管理；所有匯入均先由後端解析與預覽，再儲存草稿。
          </p>
        </div>
        <Button
          type="button"
          variant="secondary"
          disabled={pending || !market}
          onClick={() => void refresh()}
        >
          <RefreshCw className={pending ? "animate-spin" : ""} /> 重新整理
        </Button>
      </header>

      <Card className="gap-4 p-4">
        <div className="grid gap-3 sm:grid-cols-2">
          <div className="grid gap-1.5">
            <Label htmlFor="calendar-market">市場</Label>
            <select
              id="calendar-market"
              className="h-10 rounded-lg border border-line bg-surface px-3 text-sm"
              value={market}
              onChange={event => selectMarket(event.target.value)}
              disabled={initialLoading || pending}
            >
              {markets.map(item => (
                <option key={item.market} value={item.market}>
                  {item.display_name}（{item.market}）
                </option>
              ))}
            </select>
          </div>
          <div className="grid gap-1.5">
            <Label htmlFor="calendar-year">年度</Label>
            <Input
              id="calendar-year"
              type="number"
              min="1900"
              max="2200"
              value={year}
              onChange={event => selectYear(Number(event.target.value))}
              disabled={initialLoading || pending}
            />
          </div>
        </div>
        {calendar && (
          <div className="flex flex-wrap items-center gap-2 text-sm">
            <Badge
              variant={calendar.published_revision ? "default" : "warning"}
            >
              {revisionText(calendar)}
            </Badge>
            <Badge variant="outline">{calendar.timezone}</Badge>
            {calendar.published_revision && (
              <span className="text-muted">
                最後發布：{formatDate(calendar.published_revision.published_at)}
              </span>
            )}
          </div>
        )}
      </Card>

      {error && (
        <Alert variant="destructive" role="alert">
          <AlertTitle>無法更新交易日曆</AlertTitle>
          <AlertDescription>{error}</AlertDescription>
        </Alert>
      )}
      {initialLoading ? (
        <CalendarSkeleton />
      ) : calendar ? (
        <>
          <Summary summary={calendar.summary} />
          <nav className="flex flex-wrap gap-2" aria-label="交易日曆功能">
            {TABS.map(item => (
              <Button
                key={item.id}
                type="button"
                size="sm"
                variant={tab === item.id ? "default" : "secondary"}
                onClick={() => setTab(item.id)}
              >
                {item.label}
              </Button>
            ))}
          </nav>
          {tab === "year" && (
            <YearView
              calendar={calendar}
              canEdit={canEdit}
              role={role}
              search={search}
              updateSearch={updateSearch}
              onSaved={() => void refresh()}
              onRefresh={() => void refresh()}
            />
          )}
          {tab === "manual" && (
            <ManualPanel
              calendar={calendar}
              canEdit={canEdit}
              role={role}
              onSaved={() => void refresh()}
            />
          )}
          {tab === "json" && (
            <JsonImport
              market={market}
              year={year}
              calendar={calendar}
              canEdit={canEdit}
              onApplied={() => void refresh()}
            />
          )}
          {tab === "csv" && (
            <CsvImport
              market={market}
              year={year}
              calendar={calendar}
              canEdit={canEdit}
              onApplied={() => void refresh()}
            />
          )}
          {tab === "history" && (
            <CalendarHistory
              calendar={calendar}
              imports={imports}
              revisions={revisions}
              loading={pending}
              canRollback={canPublish}
              onRolledBack={() => void refresh()}
            />
          )}
          {canPublish && (
            <PublishButton
              calendar={calendar}
              onPublished={() => void refresh()}
            />
          )}
        </>
      ) : (
        !error && (
          <Alert role="status">
            <AlertDescription>
              此市場年度尚無可檢視的交易日曆。
            </AlertDescription>
          </Alert>
        )
      )}
    </div>
  )
}

function CalendarSkeleton() {
  return (
    <div className="grid gap-3 sm:grid-cols-4" role="status" aria-live="polite">
      <span className="sr-only">正在載入交易日曆</span>
      {Array.from({ length: 4 }).map((_, index) => (
        <Skeleton key={index} className="h-24 rounded-xl" />
      ))}
    </div>
  )
}

function Summary({ summary }: { summary: CalendarYear["summary"] }) {
  return (
    <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-5">
      {[
        ["全年", summary.total],
        ["開市", summary.open],
        ["休市", summary.closed],
        ["僅結算", summary.settlement_only],
        ["警告", summary.warnings],
      ].map(([label, value]) => (
        <Card className="gap-1 p-4" key={String(label)}>
          <span className="text-xs text-muted">{label}</span>
          <strong className="font-mono text-2xl">{value}</strong>
        </Card>
      ))}
    </div>
  )
}

function YearView({
  calendar,
  canEdit,
  role,
  search,
  updateSearch,
  onSaved,
  onRefresh,
}: {
  calendar: CalendarYear
  canEdit: boolean
  role: AdminRole
  search: CalendarSearch
  updateSearch: (next: CalendarSearch) => void
  onSaved: () => void
  onRefresh: () => void
}) {
  const [editing, setEditing] = useState<CalendarDay | null>(null)
  const days = useMemo(
    () =>
      calendar.days.filter(
        day =>
          (!search.month || day.date.slice(5, 7) === search.month) &&
          (!search.status || day.status === search.status) &&
          (!search.q ||
            `${day.date} ${calendarDayName(day)} ${day.description ?? ""}`
              .toLowerCase()
              .includes(search.q.toLowerCase()))
      ),
    [calendar.days, search.month, search.q, search.status]
  )
  const columns = useMemo<ColumnDef<CalendarDay, unknown>[]>(
    () => [
      {
        accessorKey: "date",
        header: "日期",
        enableSorting: false,
        cell: info => (
          <span className="font-mono">{String(info.getValue())}</span>
        ),
        meta: { width: 112, pin: "left" },
      },
      {
        id: "weekday",
        header: "星期",
        enableSorting: false,
        cell: info => weekday(info.row.original.date),
        meta: { width: 72 },
      },
      {
        accessorKey: "status",
        header: "狀態",
        enableSorting: false,
        cell: info => (
          <Badge variant={statusVariant(info.row.original.status)}>
            {calendarStatusLabel(info.row.original.status)}
          </Badge>
        ),
        meta: { width: 96 },
      },
      {
        id: "session",
        header: "時段",
        enableSorting: false,
        cell: info => {
          const day = info.row.original
          return day.session_open && day.session_close
            ? `${day.session_open}-${day.session_close}`
            : "—"
        },
        meta: { width: 128 },
      },
      {
        id: "description",
        header: "名稱／說明",
        enableSorting: false,
        cell: info => {
          const day = info.row.original
          return (
            <div>
              <strong>{calendarDayName(day)}</strong>
              {day.description ? (
                <p className="mt-1 mb-0 text-xs text-muted whitespace-pre-wrap">
                  {day.description}
                </p>
              ) : null}
            </div>
          )
        },
        meta: { width: 320, minWidth: 240, wrap: true },
      },
      {
        accessorKey: "source_kind",
        header: "來源",
        enableSorting: false,
        cell: info => String(info.getValue() ?? "—"),
        meta: { width: 140 },
      },
      ...(canEdit
        ? ([
            {
              id: "actions",
              header: () => <span className="sr-only">編輯</span>,
              enableSorting: false,
              cell: info => (
                <Button
                  type="button"
                  size="icon-xs"
                  variant="ghost"
                  aria-label={`編輯 ${info.row.original.date}`}
                  onClick={() => setEditing(info.row.original)}
                >
                  <Pencil aria-hidden="true" />
                </Button>
              ),
              meta: {
                width: 56,
                align: "right" as const,
                pin: "right" as const,
              },
            },
          ] satisfies ColumnDef<CalendarDay, unknown>[])
        : []),
    ],
    [canEdit]
  )
  return (
    <Card className="gap-4 p-4">
      <CardHeader className="px-0">
        <CardTitle className="flex items-center gap-2">
          <CalendarDays size={19} /> 全年列表{" "}
          <span className="text-sm font-normal text-muted">
            顯示 {days.length} / {calendar.days.length} 天
          </span>
        </CardTitle>
      </CardHeader>
      <CardContent className="px-0">
        <div className="mb-4 grid gap-2 sm:grid-cols-3">
          <select
            aria-label="月份篩選"
            className="h-10 rounded-lg border border-line bg-surface px-3 text-sm"
            value={search.month}
            onChange={event =>
              updateSearch({
                ...search,
                month: event.target.value as CalendarSearch["month"],
              })
            }
          >
            <option value="">全部月份</option>
            {Array.from({ length: 12 }).map((_, index) => {
              const value = String(index + 1).padStart(2, "0")
              return (
                <option key={value} value={value}>
                  {index + 1} 月
                </option>
              )
            })}
          </select>
          <select
            aria-label="狀態篩選"
            className="h-10 rounded-lg border border-line bg-surface px-3 text-sm"
            value={search.status}
            onChange={event =>
              updateSearch({
                ...search,
                status: event.target.value as CalendarSearch["status"],
              })
            }
          >
            <option value="">全部狀態</option>
            <option value="open">開市</option>
            <option value="closed">休市</option>
            <option value="settlement_only">僅結算</option>
          </select>
          <Input
            aria-label="搜尋日期或說明"
            placeholder="搜尋日期、名稱或說明"
            value={search.q}
            onChange={event =>
              updateSearch({ ...search, q: event.target.value })
            }
          />
        </div>
        <DataTable
          ariaLabel="全年交易日曆"
          columns={columns}
          data={days}
          emptyState="沒有符合條件的交易日。"
          getRowId={day => day.date}
          viewportClassName="max-h-[70dvh]"
        />
        {editing && (
          <EditDayDialog
            day={editing}
            calendar={calendar}
            role={role}
            onClose={() => setEditing(null)}
            onSaved={() => {
              onSaved()
              setEditing(null)
              onRefresh()
            }}
          />
        )}
      </CardContent>
    </Card>
  )
}

function ManualPanel({
  calendar,
  canEdit,
  role,
  onSaved,
}: {
  calendar: CalendarYear
  canEdit: boolean
  role: AdminRole
  onSaved: () => void
}) {
  const [selected, setSelected] = useState(calendar.days[0]?.date ?? "")
  const day = calendar.days.find(item => item.date === selected)
  return (
    <Card className="gap-4 p-4">
      <CardHeader className="px-0">
        <CardTitle>手動管理</CardTitle>
      </CardHeader>
      <CardContent className="grid gap-3 px-0">
        <Label htmlFor="manual-day">選擇日期</Label>
        <Input
          id="manual-day"
          type="date"
          min={`${calendar.year}-01-01`}
          max={`${calendar.year}-12-31`}
          value={selected}
          onChange={event => setSelected(event.target.value)}
        />
        {day ? (
          canEdit ? (
            <EditDayDialog
              inline
              day={day}
              calendar={calendar}
              role={role}
              onClose={() => undefined}
              onSaved={onSaved}
            />
          ) : (
            <Alert>
              <AlertDescription>
                目前帳號僅可檢視，不能修改日期。
              </AlertDescription>
            </Alert>
          )
        ) : (
          <Alert variant="warning">
            <AlertDescription>請選擇此年度內的日期。</AlertDescription>
          </Alert>
        )}
      </CardContent>
    </Card>
  )
}

function EditDayDialog({
  day,
  calendar,
  role,
  onClose,
  onSaved,
  inline = false,
}: {
  day: CalendarDay
  calendar: CalendarYear
  role: AdminRole
  onClose: () => void
  onSaved: () => void
  inline?: boolean
}) {
  const edit = useServerFn(editCalendarDay)
  const [status, setStatus] = useState(day.status)
  const [name, setName] = useState(
    calendarDayName(day) === "—" ? "" : calendarDayName(day)
  )
  const [description, setDescription] = useState(day.description ?? "")
  const [open, setOpen] = useState(day.session_open ?? "")
  const [close, setClose] = useState(day.session_close ?? "")
  const [reason, setReason] = useState("")
  const [pending, setPending] = useState(false)
  const [error, setError] = useState("")
  async function submit(event: FormEvent) {
    event.preventDefault()
    setPending(true)
    setError("")
    try {
      await edit({
        data: {
          market: calendar.market,
          date: day.date,
          expected_revision: calendar.current_revision,
          status,
          name: name || null,
          description: description || null,
          session_open: open || null,
          session_close: close || null,
          reason,
        },
      })
      onSaved()
      toast.success("已更新草稿", {
        description: `${day.date} 已建立新修訂版。`,
      })
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "無法儲存修改。")
    } finally {
      setPending(false)
    }
  }
  const form = (
    <form className="grid gap-3" onSubmit={submit}>
      <div className="flex items-center justify-between gap-2">
        <strong>
          {day.date}（{weekday(day.date)}）
        </strong>
        {!inline && (
          <Button type="button" size="sm" variant="ghost" onClick={onClose}>
            關閉
          </Button>
        )}
      </div>
      {error && (
        <Alert variant="destructive">
          <AlertDescription>{error}</AlertDescription>
        </Alert>
      )}
      <div className="grid gap-1.5">
        <Label htmlFor={`day-status-${day.date}`}>狀態</Label>
        <select
          id={`day-status-${day.date}`}
          className="h-10 rounded-lg border border-line bg-surface px-3 text-sm"
          value={status}
          onChange={event => setStatus(event.target.value as CalendarDayStatus)}
        >
          <option value="open">開市</option>
          <option value="closed">休市</option>
          <option value="settlement_only">僅結算</option>
        </select>
      </div>
      <div className="grid gap-1.5">
        <Label htmlFor={`day-name-${day.date}`}>名稱</Label>
        <Input
          id={`day-name-${day.date}`}
          maxLength={200}
          value={name}
          onChange={event => setName(event.target.value)}
        />
      </div>
      <div className="grid gap-1.5">
        <Label htmlFor={`day-description-${day.date}`}>說明（純文字）</Label>
        <textarea
          id={`day-description-${day.date}`}
          className="min-h-20 rounded-lg border border-line bg-surface p-3 text-sm"
          maxLength={2000}
          value={description}
          onChange={event => setDescription(event.target.value)}
        />
      </div>
      <div className="grid gap-3 sm:grid-cols-2">
        <div className="grid gap-1.5">
          <Label htmlFor={`day-open-${day.date}`}>開市時間</Label>
          <Input
            id={`day-open-${day.date}`}
            type="time"
            value={open}
            onChange={event => setOpen(event.target.value)}
          />
        </div>
        <div className="grid gap-1.5">
          <Label htmlFor={`day-close-${day.date}`}>收市時間</Label>
          <Input
            id={`day-close-${day.date}`}
            type="time"
            value={close}
            onChange={event => setClose(event.target.value)}
          />
        </div>
      </div>
      <div className="grid gap-1.5">
        <Label htmlFor={`day-reason-${day.date}`}>修改理由</Label>
        <Input
          id={`day-reason-${day.date}`}
          minLength={3}
          maxLength={500}
          required
          value={reason}
          onChange={event => setReason(event.target.value)}
        />
      </div>
      <Button type="submit" disabled={pending || role === "viewer"}>
        {pending ? "儲存中…" : "儲存草稿"}
      </Button>
    </form>
  )
  if (inline) return form
  return (
    <div
      className="fixed inset-0 z-50 grid place-items-center bg-black/40 p-4"
      role="dialog"
      aria-modal="true"
      aria-label={`編輯 ${day.date}`}
    >
      <Card className="max-h-[90vh] w-full max-w-xl overflow-y-auto p-5">
        {form}
      </Card>
    </div>
  )
}

function JsonImport({
  market,
  year,
  calendar,
  canEdit,
  onApplied,
}: {
  market: string
  year: number
  calendar: CalendarYear
  canEdit: boolean
  onApplied: () => void
}) {
  const previewFn = useServerFn(previewCalendarJson)
  const apply = useServerFn(applyCalendarPreview)
  const [content, setContent] = useState(
    `{\n  "market": "${market}",\n  "year": ${year},\n  "coverage_mode": "exceptions",\n  "days": []\n}`
  )
  const [preview, setPreview] = useState<CalendarPreview | null>(null)
  const [pending, setPending] = useState(false)
  const [error, setError] = useState("")
  useEffect(() => {
    setPreview(null)
    setContent(
      `{\n  "market": "${market}",\n  "year": ${year},\n  "coverage_mode": "exceptions",\n  "days": []\n}`
    )
  }, [market, year])
  async function submit(event: FormEvent) {
    event.preventDefault()
    setPending(true)
    setError("")
    try {
      setPreview(
        withPreviewDiff(
          await previewFn({ data: { market, year, content } }),
          calendar
        )
      )
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "JSON 預覽失敗。")
    } finally {
      setPending(false)
    }
  }
  return (
    <ImportPanel
      title="JSON 匯入"
      description="後端會以 canonical schema 驗證與展開全年；瀏覽器不解析內容。"
      preview={preview}
      error={error}
      pending={pending}
      canEdit={canEdit}
      onApply={async () => {
        if (!preview) return
        setPending(true)
        try {
          await apply({
            data: {
              batch_id: preview.batch_id,
              expected_revision: preview.base_revision,
            },
          })
          onApplied()
          toast.success("草稿已套用")
        } catch (cause) {
          setError(cause instanceof Error ? cause.message : "無法套用草稿。")
        } finally {
          setPending(false)
        }
      }}
    >
      <form className="grid gap-3" onSubmit={submit}>
        <Label htmlFor="calendar-json">Calendar JSON</Label>
        <textarea
          id="calendar-json"
          className="min-h-64 rounded-lg border border-line bg-surface p-3 font-mono text-sm"
          maxLength={1_000_000}
          value={content}
          onChange={event => setContent(event.target.value)}
          disabled={!canEdit || pending}
        />
        <Button type="submit" disabled={!canEdit || pending}>
          <FileJson2 />
          {pending ? "預覽中…" : "送出後端預覽"}
        </Button>
      </form>
    </ImportPanel>
  )
}

function CsvImport({
  market,
  year,
  calendar,
  canEdit,
  onApplied,
}: {
  market: string
  year: number
  calendar: CalendarYear
  canEdit: boolean
  onApplied: () => void
}) {
  const previewFn = useServerFn(previewCalendarCsv)
  const apply = useServerFn(applyCalendarPreview)
  const [file, setFile] = useState<File | null>(null)
  const [preview, setPreview] = useState<CalendarPreview | null>(null)
  const [pending, setPending] = useState(false)
  const [error, setError] = useState("")
  function choose(event: ChangeEvent<HTMLInputElement>) {
    setFile(event.target.files?.[0] ?? null)
    setPreview(null)
  }
  async function submit(event: FormEvent) {
    event.preventDefault()
    if (!file) return
    setPending(true)
    setError("")
    try {
      const form = new FormData()
      form.set("market", market)
      form.set("year", String(year))
      form.set("file", file)
      setPreview(withPreviewDiff(await previewFn({ data: form }), calendar))
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "CSV 預覽失敗。")
    } finally {
      setPending(false)
    }
  }
  return (
    <ImportPanel
      title="CSV 上傳"
      description="TW 市場預設支援證交所開（休）市格式。只顯示檔名與大小，實際編碼與內容由後端解析。"
      preview={preview}
      error={error}
      pending={pending}
      canEdit={canEdit}
      onApply={async () => {
        if (!preview) return
        setPending(true)
        try {
          await apply({
            data: {
              batch_id: preview.batch_id,
              expected_revision: preview.base_revision,
            },
          })
          onApplied()
          toast.success("草稿已套用")
        } catch (cause) {
          setError(cause instanceof Error ? cause.message : "無法套用草稿。")
        } finally {
          setPending(false)
        }
      }}
    >
      <form className="grid gap-3" onSubmit={submit}>
        <Label htmlFor="calendar-csv">CSV 檔案（最大 1 MiB）</Label>
        <Input
          id="calendar-csv"
          type="file"
          accept=".csv,text/csv"
          onChange={choose}
          disabled={!canEdit || pending}
        />
        {file && (
          <p className="m-0 text-sm text-muted">
            {file.name} · {Math.ceil(file.size / 1024)} KiB
          </p>
        )}
        <Button type="submit" disabled={!canEdit || pending || !file}>
          <FileUp />
          {pending ? "預覽中…" : "送出後端預覽"}
        </Button>
      </form>
    </ImportPanel>
  )
}

function ImportPanel({
  title,
  description,
  children,
  preview,
  error,
  pending,
  canEdit,
  onApply,
}: {
  title: string
  description: string
  children: React.ReactNode
  preview: CalendarPreview | null
  error: string
  pending: boolean
  canEdit: boolean
  onApply: () => Promise<void>
}) {
  const blocking = Boolean(preview?.errors.length)
  return (
    <Card className="gap-4 p-4">
      <CardHeader className="px-0">
        <CardTitle>{title}</CardTitle>
        <p className="text-sm text-muted">{description}</p>
      </CardHeader>
      <CardContent className="grid gap-4 px-0">
        {error && (
          <Alert variant="destructive">
            <AlertDescription>{error}</AlertDescription>
          </Alert>
        )}
        {children}
        {preview && (
          <>
            <PreviewDetails preview={preview} />
            <Button
              type="button"
              disabled={!canEdit || pending || blocking}
              onClick={() => void onApply()}
            >
              <Send />
              {blocking
                ? "請先排除 blocking errors"
                : pending
                  ? "套用中…"
                  : "確認並儲存草稿"}
            </Button>
          </>
        )}
      </CardContent>
    </Card>
  )
}

function withPreviewDiff(preview: CalendarPreview, calendar: CalendarYear) {
  const current = new Map(calendar.days.map(day => [day.date, day]))
  let added = 0
  let changed = 0
  let unchanged = 0
  for (const candidate of preview.days) {
    const existing = current.get(candidate.date)
    if (!existing) added += 1
    else if (
      existing.status === candidate.status &&
      calendarDayName(existing) === calendarDayName(candidate) &&
      (existing.description ?? "") === (candidate.description ?? "") &&
      (existing.session_open ?? "") === (candidate.session_open ?? "") &&
      (existing.session_close ?? "") === (candidate.session_close ?? "")
    )
      unchanged += 1
    else changed += 1
  }
  return { ...preview, diff: { added, changed, unchanged, conflicts: 0 } }
}

function PreviewDetails({ preview }: { preview: CalendarPreview }) {
  return (
    <section
      className="grid gap-3 rounded-xl border border-line bg-surface-soft p-4"
      aria-live="polite"
    >
      <div className="flex flex-wrap gap-2">
        <Badge variant="outline">parser: {preview.parser_id}</Badge>
        {preview.detected_encoding && (
          <Badge variant="outline">編碼：{preview.detected_encoding}</Badge>
        )}
        {preview.inferred_year && (
          <Badge variant="outline">推導年度：{preview.inferred_year}</Badge>
        )}
        <Badge variant={preview.errors.length ? "destructive" : "default"}>
          {preview.errors.length ? `${preview.errors.length} 個錯誤` : "可套用"}
        </Badge>
      </div>
      <p className="m-0 text-sm">
        全年 {preview.summary.total} 天：開市 {preview.summary.open}、休市{" "}
        {preview.summary.closed}、僅結算 {preview.summary.settlement_only}。
      </p>
      <p className="m-0 text-sm text-muted">
        差異：新增 {preview.diff.added}、變更 {preview.diff.changed}、未變{" "}
        {preview.diff.unchanged}、衝突 {preview.diff.conflicts}。
      </p>
      {[...preview.errors, ...preview.warnings]
        .slice(0, 20)
        .map((issue, index) => (
          <Alert
            key={`${issue.row ?? ""}-${issue.field ?? ""}-${index}`}
            variant={index < preview.errors.length ? "destructive" : "warning"}
          >
            <AlertDescription>
              {issue.row ? `第 ${issue.row} 列：` : ""}
              {issue.message}
            </AlertDescription>
          </Alert>
        ))}
    </section>
  )
}

function CalendarHistory({
  calendar,
  imports,
  revisions,
  loading,
  canRollback,
  onRolledBack,
}: {
  calendar: CalendarYear
  imports: CalendarImport[]
  revisions: CalendarRevision[]
  loading: boolean
  canRollback: boolean
  onRolledBack: () => void
}) {
  const rollback = useServerFn(rollbackCalendarYear)
  const [rollingBack, setRollingBack] = useState<number | null>(null)
  const [error, setError] = useState("")

  async function submit(targetRevision: number) {
    if (
      !window.confirm(
        `確定以 r${targetRevision} 的內容建立新的已發布修訂版？現行發布版本會保留為歷史紀錄。`
      )
    )
      return
    setRollingBack(targetRevision)
    setError("")
    try {
      await rollback({
        data: {
          market: calendar.market,
          year: calendar.year,
          target_revision: targetRevision,
          expected_revision: calendar.current_revision,
        },
      })
      onRolledBack()
      toast.success(`已回滾至 r${targetRevision} 的內容`)
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "回滾失敗。")
    } finally {
      setRollingBack(null)
    }
  }

  const columns = useMemo<ColumnDef<CalendarRevision, unknown>[]>(
    () => [
      {
        accessorKey: "revision",
        header: "修訂版",
        enableSorting: false,
        cell: info => (
          <span className="font-mono">r{String(info.getValue())}</span>
        ),
        meta: { width: 88, pin: "left" },
      },
      {
        accessorKey: "status",
        header: "狀態",
        enableSorting: false,
        cell: info => {
          const status = info.row.original.status
          return (
            <Badge
              variant={
                status === "published"
                  ? "default"
                  : status === "draft"
                    ? "warning"
                    : "secondary"
              }
            >
              {status}
            </Badge>
          )
        },
        meta: { width: 112 },
      },
      {
        accessorKey: "source_kind",
        header: "來源",
        enableSorting: false,
        meta: { width: 160 },
      },
      {
        accessorKey: "updated_at",
        header: "更新時間",
        enableSorting: false,
        cell: info => formatDate(info.row.original.updated_at),
        meta: { width: 180 },
      },
      ...(canRollback
        ? ([
            {
              id: "actions",
              header: "操作",
              enableSorting: false,
              cell: info => {
                const item = info.row.original
                return (
                  <Button
                    type="button"
                    size="sm"
                    variant="secondary"
                    disabled={
                      rollingBack !== null ||
                      item.status !== "superseded" ||
                      !item.coverage_complete
                    }
                    onClick={() => void submit(item.revision)}
                  >
                    {rollingBack === item.revision ? "回滾中…" : "以此版本回滾"}
                  </Button>
                )
              },
              meta: {
                width: 132,
                align: "right" as const,
                pin: "right" as const,
              },
            },
          ] satisfies ColumnDef<CalendarRevision, unknown>[])
        : []),
    ],
    [canRollback, rollingBack]
  )

  return (
    <div className="grid gap-4">
      <Card className="gap-4 p-4">
        <CardHeader className="px-0">
          <CardTitle>年度修訂紀錄</CardTitle>
        </CardHeader>
        <CardContent className="grid gap-3 px-0">
          {error && (
            <Alert variant="destructive">
              <AlertDescription>{error}</AlertDescription>
            </Alert>
          )}
          <DataTable
            ariaLabel="年度修訂紀錄"
            columns={columns}
            data={revisions}
            emptyState="此市場年度尚無修訂紀錄。"
            getRowId={item => String(item.revision)}
            isLoading={loading && revisions.length === 0}
            isRefreshing={loading && revisions.length > 0}
            viewportClassName="max-h-96"
          />
        </CardContent>
      </Card>
      <ImportHistory imports={imports} loading={loading} />
    </div>
  )
}

function ImportHistory({
  imports,
  loading,
}: {
  imports: CalendarImport[]
  loading: boolean
}) {
  const columns = useMemo<ColumnDef<CalendarImport, unknown>[]>(
    () => [
      {
        accessorKey: "created_at",
        header: "時間",
        enableSorting: false,
        cell: info => formatDate(info.row.original.created_at),
        meta: { width: 180, pin: "left" },
      },
      {
        accessorKey: "input_format",
        header: "格式",
        enableSorting: false,
        meta: { width: 100 },
      },
      {
        id: "source",
        header: "檔案／來源",
        enableSorting: false,
        cell: info =>
          info.row.original.source_filename ??
          info.row.original.source_sha256 ??
          "—",
        meta: { width: 300, minWidth: 220, wrap: true },
      },
      {
        accessorKey: "status",
        header: "狀態",
        enableSorting: false,
        meta: { width: 120 },
      },
      {
        accessorKey: "revision",
        header: "修訂版",
        enableSorting: false,
        cell: info => String(info.getValue() ?? "—"),
        meta: { width: 96, align: "right" },
      },
    ],
    []
  )
  return (
    <Card className="gap-4 p-4">
      <CardHeader className="px-0">
        <CardTitle>匯入紀錄</CardTitle>
      </CardHeader>
      <CardContent className="px-0">
        <DataTable
          ariaLabel="日曆匯入紀錄"
          columns={columns}
          data={imports}
          emptyState="此市場年度尚無匯入紀錄。"
          getRowId={item => item.id}
          isLoading={loading && imports.length === 0}
          isRefreshing={loading && imports.length > 0}
          viewportClassName="max-h-96"
        />
      </CardContent>
    </Card>
  )
}

function PublishButton({
  calendar,
  onPublished,
}: {
  calendar: CalendarYear
  onPublished: () => void
}) {
  const publish = useServerFn(publishCalendarYear)
  const [pending, setPending] = useState(false)
  const [error, setError] = useState("")
  async function submit() {
    if (
      !window.confirm(
        `確定發布 ${calendar.market} ${calendar.year} 年的交易日曆？Scheduler 將以此已發布修訂版運作。`
      )
    )
      return
    setPending(true)
    setError("")
    try {
      await publish({
        data: {
          market: calendar.market,
          year: calendar.year,
          expected_revision: calendar.current_revision,
        },
      })
      onPublished()
      toast.success("年度日曆已發布")
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "發布失敗。")
    } finally {
      setPending(false)
    }
  }
  return (
    <section className="flex flex-wrap items-center justify-end gap-3">
      <span className="text-sm text-danger">{error}</span>
      <Button
        type="button"
        disabled={pending || !calendar.coverage_complete}
        onClick={() => void submit()}
      >
        <Upload />
        {pending ? "發布中…" : "發布年度日曆"}
      </Button>
      {!calendar.coverage_complete && (
        <span className="text-sm text-muted">
          需先完成 365／366 天覆蓋才能發布。
        </span>
      )}
    </section>
  )
}
