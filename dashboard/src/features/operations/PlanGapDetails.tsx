import { useState } from "react"
import { useQuery } from "@tanstack/react-query"
import { useServerFn } from "@tanstack/react-start"
import { QueryStatus } from "../../components/AsyncState"
import { useProtectedQueryScope } from "../../components/ProtectedQueryScope"
import {
  manualQueryOptions,
  useRegisterOperationsQuery,
} from "../../components/OperationsRefresh"
import { Button } from "../../components/ui/button"
import { Label } from "../../components/ui/label"
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "../../components/ui/select"
import { loadDeliveryPlanSummary } from "../../lib/admin.functions"
import type { GapStatus } from "../../lib/admin-api"
import { withName } from "../../lib/vocabulary"

export const reasonLabels: Record<string, string> = {
  mapping_gap: "供應商未收錄",
  not_yet_trading: "新掛牌尚未交易",
  no_data_exhausted: "重試用完仍無資料",
  quota: "額度不足",
  rate_limited: "來源請求頻率受限",
  halted: "暫停交易",
  no_trade: "無成交紀錄",
  pending: "尚待交付",
  permission_denied: "權限不足",
  source_error: "來源錯誤",
}

const statusLabels: Record<GapStatus, string> = {
  missing: "缺漏",
  blocked: "受阻",
  unavailable: "無法取得",
}

const PAGE_SIZE = 200

function reasonText(reason: string) {
  return withName(reasonLabels, reason)
}

/**
 * Which members a plan is missing and why. Listing them classifies the whole
 * plan on the server, so it loads only when asked for and is never polled.
 */
export function PlanGapDetails({
  planId,
  missing,
  blocked,
  unavailable,
  unlisted,
}: {
  planId: string
  missing: number
  blocked: number
  unavailable: number
  unlisted: number
}) {
  const loadSummary = useServerFn(loadDeliveryPlanSummary)
  const scope = useProtectedQueryScope()
  const [requested, setRequested] = useState(false)
  const [status, setStatus] = useState<GapStatus | "all">("all")
  const [page, setPage] = useState(1)
  const hasDetails = missing + blocked + unavailable + unlisted > 0
  const request = {
    planId,
    gapStatus: status === "all" ? [] : [status],
    gapPage: page,
    gapPageSize: PAGE_SIZE,
  }
  const queryKey = [
    "operations",
    "delivery-plan-summary",
    scope,
    request,
  ] as const
  const enabled = requested && hasDetails
  useRegisterOperationsQuery(queryKey, enabled)
  const details = useQuery({
    ...manualQueryOptions,
    queryKey,
    queryFn: () => loadSummary({ data: request }),
    enabled,
  })
  if (!hasDetails)
    return (
      <p className="text-sm text-muted">此計畫沒有缺口，也沒有清單外資料。</p>
    )
  if (!requested)
    return (
      <Button
        type="button"
        variant="secondary"
        size="sm"
        onClick={() => setRequested(true)}
      >
        載入缺口明細
      </Button>
    )
  const total = details.data?.gaps_total ?? details.data?.gaps.length ?? 0
  const pages = Math.max(Math.ceil(total / PAGE_SIZE), 1)
  return (
    <div className="space-y-3">
      <div className="space-y-1">
        <Label htmlFor={`gap-status-${planId}`}>缺口狀態</Label>
        <Select
          value={status}
          onValueChange={value => {
            setStatus(value as GapStatus | "all")
            setPage(1)
          }}
        >
          <SelectTrigger id={`gap-status-${planId}`} size="sm">
            <SelectValue>
              {status === "all" ? "全部" : statusLabels[status]}
            </SelectValue>
          </SelectTrigger>
          <SelectContent>
            <SelectItem value="all">全部</SelectItem>
            {(Object.keys(statusLabels) as GapStatus[]).map(key => (
              <SelectItem key={key} value={key}>
                {statusLabels[key]}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
      </div>
      <QueryStatus
        hasData={details.data !== undefined}
        pending={details.isFetching}
        error={details.error}
        updatedAt={details.dataUpdatedAt}
        onRetry={() => void details.refetch({ cancelRefetch: false })}
        label="缺口明細"
      />
      {details.data && (
        <>
          <p className="text-sm font-medium">
            共 {total} 筆{pages > 1 ? `，第 ${page}／${pages} 頁` : ""}
          </p>
          {details.data.gaps.length > 0 ? (
            <ul className="max-h-72 space-y-1 overflow-auto text-xs">
              {details.data.gaps.map(gap => (
                <li key={`${gap.member_key}:${gap.status}`}>
                  <span className="font-mono">{gap.member_key}</span>：
                  {statusLabels[gap.status as GapStatus] ?? gap.status}
                  {gap.reason && <span> · {reasonText(gap.reason)}</span>}
                </li>
              ))}
            </ul>
          ) : (
            <p className="text-sm text-muted">沒有符合條件的缺口。</p>
          )}
          {pages > 1 && (
            <div className="flex gap-2">
              <Button
                type="button"
                variant="outline"
                size="sm"
                disabled={page <= 1}
                onClick={() => setPage(page - 1)}
              >
                上一頁
              </Button>
              <Button
                type="button"
                variant="outline"
                size="sm"
                disabled={page >= pages}
                onClick={() => setPage(page + 1)}
              >
                下一頁
              </Button>
            </div>
          )}
          {details.data.unlisted.length > 0 && (
            <div className="space-y-1">
              <p className="text-sm font-medium">
                清單外有資料：共 {unlisted} 檔
                {details.data.unlisted.length < unlisted
                  ? `（列出前 ${details.data.unlisted.length} 檔）`
                  : ""}
              </p>
              <p className="max-h-32 overflow-auto break-all font-mono text-xs">
                {details.data.unlisted.join("、")}
              </p>
            </div>
          )}
        </>
      )}
    </div>
  )
}
