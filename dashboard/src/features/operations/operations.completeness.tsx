import { useQuery } from "@tanstack/react-query"
import { Link } from "@tanstack/react-router"
import { useServerFn } from "@tanstack/react-start"
import { Grid3x3 } from "lucide-react"
import { ContentSkeleton, QueryStatus } from "../../components/AsyncState"
import { useProtectedQueryScope } from "../../components/ProtectedQueryScope"
import {
  liveQueryOptions,
  useRegisterOperationsQuery,
} from "../../components/OperationsRefresh"
import { Badge } from "../../components/ui/badge"
import { Input } from "../../components/ui/input"
import { Label } from "../../components/ui/label"
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "../../components/ui/select"
import { loadCompleteness } from "../../lib/admin.functions"
import type {
  CompletenessCell,
  CompletenessRow,
  CompletenessState,
} from "../../lib/admin-api"
import type { AdminRole } from "../../lib/admin-governance-api"
import { PlanGapDetails } from "./PlanGapDetails"
import { formatDate, PageIntro, Panel } from "./operations.shared"
import {
  completenessSearchSchema,
  type CompletenessSearch,
} from "./operations.search"

const MAX_DAYS = 62
const WEEKDAYS = ["日", "一", "二", "三", "四", "五", "六"]

export const STATE_LABELS: Record<CompletenessState, string> = {
  complete: "完整",
  incomplete: "未完成",
  late: "逾時",
  not_due: "尚未到期",
  not_planned: "已到期但沒有計畫",
  closed: "休市或依設定略過",
  not_started: "首次抓取日之前",
}

const STATE_SYMBOLS: Record<CompletenessState, string> = {
  complete: "●",
  incomplete: "◑",
  late: "◐",
  not_due: "○",
  not_planned: "✕",
  closed: "·",
  not_started: "",
}

// States that need someone's attention; "只顯示有缺口" keeps rows with any.
const GAP_STATES = new Set<CompletenessState>([
  "incomplete",
  "late",
  "not_planned",
])

function gapCount(cell: CompletenessCell) {
  return cell.counts ? cell.counts.missing + cell.counts.blocked : 0
}

function rangeProblem(from: string, to: string) {
  if (!from || !to) return ""
  const days =
    (new Date(`${to}T00:00:00Z`).getTime() -
      new Date(`${from}T00:00:00Z`).getTime()) /
      86_400_000 +
    1
  if (days < 1) return "起日不可晚於迄日。"
  if (days > MAX_DAYS) return `日期範圍最多 ${MAX_DAYS} 天。`
  return ""
}

function CellButton({
  row,
  cell,
  selected,
  onSelect,
}: {
  row: CompletenessRow
  cell: CompletenessCell
  selected: boolean
  onSelect: () => void
}) {
  const gaps = gapCount(cell)
  const state = cell.state
  return (
    <button
      type="button"
      onClick={onSelect}
      aria-pressed={selected}
      aria-label={`${row.dataset_key} ${row.provider} ${cell.trade_date}：${STATE_LABELS[state]}${gaps ? `，缺口 ${gaps}` : ""}${cell.counts_synced ? "" : "，計數同步中"}${cell.open_alert ? "，有未關閉告警" : ""}`}
      title={`${cell.trade_date} ${STATE_LABELS[state]}${gaps ? ` · 缺口 ${gaps}` : ""}`}
      className={`flex h-8 w-full min-w-11 items-center justify-center gap-0.5 rounded font-mono text-xs tabular-nums focus-visible:ring-3 focus-visible:ring-accent/30 focus-visible:outline-none ${
        state === "complete"
          ? "bg-accent-soft text-accent"
          : state === "incomplete"
            ? "bg-warning-soft text-warning"
            : state === "late" || state === "not_planned"
              ? "bg-danger-soft text-danger"
              : state === "not_due"
                ? "bg-surface-soft text-muted"
                : "text-muted"
      } ${selected ? "ring-2 ring-accent" : cell.counts_synced ? "" : "ring-1 ring-warning"}`}
    >
      <span aria-hidden="true">{STATE_SYMBOLS[state]}</span>
      {gaps > 0 && <span aria-hidden="true">{gaps}</span>}
    </button>
  )
}

function CellDetails({
  row,
  cell,
  role,
}: {
  row: CompletenessRow
  cell: CompletenessCell
  role: AdminRole
}) {
  const counts = cell.counts
  const needsWork = GAP_STATES.has(cell.state)
  return (
    <section
      aria-label="所選日期明細"
      className="space-y-4 rounded-xl border border-line bg-surface p-4"
    >
      <div className="space-y-1">
        <p className="font-mono text-sm font-bold wrap-anywhere">
          {row.dataset_key}
        </p>
        <p className="text-xs text-muted">
          {row.provider} · {row.market} · 交易日 {cell.trade_date}
        </p>
        <div className="flex flex-wrap gap-2">
          <Badge
            variant={
              cell.state === "complete"
                ? "secondary"
                : cell.state === "incomplete"
                  ? "warning"
                  : cell.state === "late" || cell.state === "not_planned"
                    ? "destructive"
                    : "outline"
            }
          >
            {STATE_LABELS[cell.state]}
          </Badge>
          {!cell.counts_synced && <Badge variant="warning">計數同步中</Badge>}
          {cell.open_alert && <Badge variant="destructive">有未關閉告警</Badge>}
        </div>
      </div>
      {cell.state === "not_planned" && (
        <p className="text-sm text-muted">
          這天應有資料，但沒有建立交付計畫：可能是排程停止、Fetcher
          未執行或規劃失敗。可帶入回補補抓。
        </p>
      )}
      {cell.state === "not_started" && (
        <p className="text-sm text-muted">
          早於首次抓取日 {row.first_start_date ?? "（尚未啟用）"}。
        </p>
      )}
      {cell.deadline_at && (
        <p className="text-sm">完成期限：{formatDate(cell.deadline_at)}</p>
      )}
      {counts && (
        <dl className="grid grid-cols-2 gap-x-4 gap-y-1 text-sm">
          {(
            [
              ["expected", "預期"],
              ["data", "有資料"],
              ["no_data", "正常無資料"],
              ["missing", "缺漏"],
              ["blocked", "受阻"],
              ["unavailable", "無法取得"],
              ["unlisted_data", "清單外有資料"],
            ] as const
          ).map(([key, label]) => (
            <div key={key} className="contents">
              <dt className="text-muted">{label}</dt>
              <dd className="text-right font-mono tabular-nums">
                {counts[key]}
              </dd>
            </div>
          ))}
        </dl>
      )}
      {!cell.counts_synced && (
        <p className="text-xs text-muted">
          已存計數還在更新，數字可能落後；交付監控會自動補正，不需手動處理。
        </p>
      )}
      {cell.plan_id && counts && (
        <PlanGapDetails
          key={cell.plan_id}
          planId={cell.plan_id}
          missing={counts.missing}
          blocked={counts.blocked}
          unavailable={counts.unavailable}
          unlisted={counts.unlisted_data}
        />
      )}
      <div className="flex flex-wrap gap-3 text-sm">
        {cell.open_alert && (
          <Link
            to="/operations/deliveries"
            search={{ tab: "alerts" }}
            className="font-bold text-accent underline-offset-4 hover:underline"
          >
            查看缺漏告警
          </Link>
        )}
        {needsWork && role !== "viewer" && (
          <Link
            to="/operations/deliveries"
            search={{
              tab: "backfills",
              seed: `${row.provider}|${row.dataset_key}|${cell.trade_date}`,
            }}
            className="font-bold text-accent underline-offset-4 hover:underline"
          >
            帶入回補
          </Link>
        )}
      </div>
    </section>
  )
}

export function CompletenessPage({
  search = completenessSearchSchema.parse({}),
  updateSearch = () => {},
  role = "viewer",
}: {
  search?: CompletenessSearch
  updateSearch?: (next: CompletenessSearch) => void
  role?: AdminRole
}) {
  const load = useServerFn(loadCompleteness)
  const scope = useProtectedQueryScope()
  const problem = rangeProblem(search.from, search.to)
  const request = { dateFrom: search.from, dateTo: search.to, datasetKey: "" }
  const queryKey = ["operations", "completeness", scope, request] as const
  useRegisterOperationsQuery(queryKey, !problem)
  const query = useQuery({
    ...liveQueryOptions,
    queryKey,
    queryFn: () => load({ data: request }),
    enabled: !problem,
  })
  const data = query.data
  const markets = Array.from(new Set(data?.rows.map(row => row.market) ?? []))
    .concat(search.market && !data ? [search.market] : [])
    .sort()
  const rows = (data?.rows ?? []).filter(
    row =>
      (!search.market || row.market === search.market) &&
      (!search.gaps || row.cells.some(cell => GAP_STATES.has(cell.state)))
  )
  const dates = data?.rows[0]?.cells.map(cell => cell.trade_date) ?? []
  const selectedRow = data?.rows.find(
    row =>
      row.dataset_key === search.dataset &&
      (!search.provider || row.provider === search.provider)
  )
  const selectedCell = selectedRow?.cells.find(
    cell => cell.trade_date === search.date
  )
  return (
    <>
      <PageIntro
        eyebrow="Completeness"
        title="資料完整性"
        description="每個資料集在每個交易日是否完整。格子只讀已存的交付統計；點選格子可查看缺口明細並帶入回補。"
      />
      <Panel
        eyebrow="Dataset × trade date"
        title="完整性矩陣"
        icon={<Grid3x3 size={19} />}
        result={{ ok: true, data: null }}
      >
        <div className="mb-3 grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
          <div className="space-y-1">
            <Label htmlFor="completeness-from">起日</Label>
            <Input
              id="completeness-from"
              type="date"
              value={search.from}
              onChange={event =>
                updateSearch({ ...search, from: event.target.value })
              }
            />
          </div>
          <div className="space-y-1">
            <Label htmlFor="completeness-to">迄日</Label>
            <Input
              id="completeness-to"
              type="date"
              value={search.to}
              onChange={event =>
                updateSearch({ ...search, to: event.target.value })
              }
            />
          </div>
          <div className="space-y-1">
            <Label htmlFor="completeness-market">市場</Label>
            <Select
              value={search.market || "all"}
              onValueChange={value =>
                updateSearch({
                  ...search,
                  market: value === "all" ? "" : value,
                })
              }
            >
              <SelectTrigger id="completeness-market" size="sm">
                <SelectValue>{search.market || "全部市場"}</SelectValue>
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="all">全部市場</SelectItem>
                {markets.map(market => (
                  <SelectItem key={market} value={market}>
                    {market}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>
          <label className="flex items-end gap-2 pb-2 text-sm">
            <input
              type="checkbox"
              checked={search.gaps}
              onChange={event =>
                updateSearch({ ...search, gaps: event.target.checked })
              }
            />
            只顯示有缺口的資料集
          </label>
        </div>
        <p className="mb-3 text-xs text-muted">
          未指定日期時顯示到今天為止的 28 天；範圍最多 {MAX_DAYS}{" "}
          天。日期是各交易所的當地交易日。
        </p>
        {problem && (
          <p role="alert" className="mb-3 text-sm text-danger">
            {problem}
          </p>
        )}
        <ul
          aria-label="圖例"
          className="mb-3 flex flex-wrap gap-x-4 gap-y-1 text-xs text-muted"
        >
          {(Object.keys(STATE_LABELS) as CompletenessState[])
            .filter(state => state !== "not_started")
            .map(state => (
              <li key={state}>
                <span aria-hidden="true" className="mr-1 font-mono">
                  {STATE_SYMBOLS[state]}
                </span>
                {STATE_LABELS[state]}
              </li>
            ))}
          <li>數字＝缺漏＋受阻檔數</li>
          <li>空白＝首次抓取日之前</li>
          <li>黃框＝計數同步中</li>
        </ul>
        <QueryStatus
          hasData={data !== undefined}
          pending={query.isFetching}
          error={query.error}
          updatedAt={query.dataUpdatedAt}
          onRetry={() => void query.refetch({ cancelRefetch: false })}
          label="完整性矩陣"
        />
        <div className="grid gap-4 xl:grid-cols-[minmax(0,1fr)_22rem] xl:items-start">
          <div className="min-w-0 overflow-x-auto rounded-lg border border-line">
            {data === undefined ? (
              query.isPending && !problem ? (
                <ContentSkeleton label="正在載入完整性矩陣" />
              ) : null
            ) : rows.length === 0 ? (
              <p className="p-4 text-sm text-muted">
                {data.rows.length === 0
                  ? "尚無全市場資料集。"
                  : "沒有符合篩選條件的資料集。"}
              </p>
            ) : (
              <table
                aria-label="完整性矩陣"
                className="w-max border-separate border-spacing-0.5 text-xs"
              >
                <thead>
                  <tr>
                    <th
                      scope="col"
                      className="sticky left-0 z-10 bg-surface px-2 text-left font-medium text-muted"
                    >
                      資料集
                    </th>
                    {dates.map(day => (
                      <th
                        key={day}
                        scope="col"
                        className="px-0.5 text-center font-medium text-muted"
                      >
                        <span className="block font-mono">{day.slice(5)}</span>
                        <span className="block">
                          {WEEKDAYS[new Date(`${day}T00:00:00Z`).getUTCDay()]}
                        </span>
                      </th>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  {rows.map(row => (
                    <tr key={`${row.dataset_key}:${row.provider}`}>
                      <th
                        scope="row"
                        className="sticky left-0 z-10 bg-surface px-2 text-left font-normal"
                      >
                        <span className="block font-mono font-bold whitespace-nowrap">
                          {row.dataset_key}
                        </span>
                        <span className="block text-muted whitespace-nowrap">
                          {row.provider} · {row.market}
                        </span>
                      </th>
                      {row.cells.map(cell => (
                        <td key={cell.trade_date} className="p-0">
                          <CellButton
                            row={row}
                            cell={cell}
                            selected={
                              row === selectedRow &&
                              cell.trade_date === search.date
                            }
                            onSelect={() =>
                              updateSearch({
                                ...search,
                                dataset: row.dataset_key,
                                provider: row.provider,
                                date: cell.trade_date,
                              })
                            }
                          />
                        </td>
                      ))}
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </div>
          {selectedRow && selectedCell ? (
            <CellDetails
              key={`${selectedRow.dataset_key}:${selectedRow.provider}:${selectedCell.trade_date}`}
              row={selectedRow}
              cell={selectedCell}
              role={role}
            />
          ) : (
            <p className="rounded-xl border border-dashed border-line p-4 text-sm text-muted">
              {search.dataset && data
                ? "所選資料集或日期不在目前範圍內，請調整日期範圍。"
                : "點選格子查看該日的計數、缺口明細與後續處理。"}
            </p>
          )}
        </div>
      </Panel>
    </>
  )
}
