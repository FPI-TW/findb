import { useQuery, useQueryClient } from "@tanstack/react-query"
import { useServerFn } from "@tanstack/react-start"
import { ClipboardList } from "lucide-react"
import { useState } from "react"

import { useProtectedQueryScope } from "../../components/ProtectedQueryScope"
import { Alert, AlertDescription } from "../../components/ui/alert"
import { Badge } from "../../components/ui/badge"
import { Button } from "../../components/ui/button"
import { Input } from "../../components/ui/input"
import { Label } from "../../components/ui/label"
import { loadDeliveryPlans } from "../../lib/admin.functions"
import type { DeliveryPlan } from "../../lib/admin-api"
import { formatDate, LoadingState, Panel } from "./operations.shared"

const statusLabel: Record<DeliveryPlan["status"], string> = {
  pending: "待執行",
  running: "執行中",
  complete: "完整",
  incomplete: "不完整",
  blocked: "受阻",
}

function PlanCard({ plan }: { plan: DeliveryPlan }) {
  const summary = plan.summary
  return (
    <article className="space-y-3 rounded-lg border border-line bg-surface p-4">
      <div className="flex flex-wrap items-start justify-between gap-2">
        <div>
          <h4 className="m-0 font-mono text-sm font-semibold">
            {plan.dataset_key}
          </h4>
          <p className="mt-1 text-xs text-muted">
            {plan.provider} · 交易日 {plan.trade_date} · 批次 {plan.release_id}
          </p>
        </div>
        <Badge variant={plan.status === "complete" ? "secondary" : "outline"}>
          {statusLabel[plan.status]}
        </Badge>
      </div>
      <p className="m-0 text-xs text-muted">
        截止：{formatDate(summary.deadline_at)}
        {summary.is_late && (
          <strong className="ml-2 text-destructive">已逾時</strong>
        )}
      </p>
      <dl className="grid grid-cols-2 gap-2 text-xs sm:grid-cols-5">
        {[
          ["預期", summary.expected],
          ["有資料", summary.data],
          ["正常無資料", summary.no_data],
          ["缺漏", summary.missing],
          ["受阻", summary.blocked],
        ].map(([label, value]) => (
          <div key={label} className="rounded-md bg-surface-soft p-2">
            <dt className="text-muted">{label}</dt>
            <dd className="mt-1 font-mono text-base font-semibold">{value}</dd>
          </div>
        ))}
      </dl>
      {summary.gaps.length > 0 && (
        <div>
          <h5 className="m-0 text-xs font-semibold">待處理缺口</h5>
          <ul className="mt-2 max-h-44 space-y-1 overflow-y-auto pl-5 text-xs">
            {summary.gaps.map((gap, index) => (
              <li key={`${gap.member_key}-${gap.status}-${index}`}>
                <span className="font-mono">{gap.member_key}</span>：
                {gap.status}
                {gap.reason ? `（${gap.reason}）` : ""}
              </li>
            ))}
          </ul>
        </div>
      )}
    </article>
  )
}

export function DeliveryPlansPanel() {
  const load = useServerFn(loadDeliveryPlans)
  const queryClient = useQueryClient()
  const scope = useProtectedQueryScope()
  const [datasetKey, setDatasetKey] = useState("")
  const [tradeDate, setTradeDate] = useState("")
  const request = { datasetKey, tradeDate, limit: 25 }
  const queryKey = ["operations", "delivery-plans", scope, request] as const
  const query = useQuery({
    queryKey,
    queryFn: () => load({ data: request }),
    retry: false,
    refetchOnWindowFocus: false,
  })

  return (
    <Panel
      className="mb-5"
      eyebrow="Full market"
      title="全市場交付計畫"
      icon={<ClipboardList size={19} />}
      result={{ ok: true, data: null }}
    >
      <div className="mb-4 grid gap-3 sm:grid-cols-[1fr_1fr_auto] sm:items-end">
        <div className="space-y-1">
          <Label htmlFor="plan-dataset">資料集</Label>
          <Input
            id="plan-dataset"
            value={datasetKey}
            onChange={event => setDatasetKey(event.target.value)}
            placeholder="篩選資料集"
          />
        </div>
        <div className="space-y-1">
          <Label htmlFor="plan-date">交易日</Label>
          <Input
            id="plan-date"
            type="date"
            value={tradeDate}
            onChange={event => setTradeDate(event.target.value)}
          />
        </div>
        <Button
          type="button"
          variant="outline"
          onClick={() => void queryClient.invalidateQueries({ queryKey })}
        >
          更新計畫
        </Button>
      </div>
      {query.isPending ? (
        <LoadingState label="正在載入全市場交付計畫" />
      ) : query.isError ? (
        <Alert variant="destructive">
          <AlertDescription>
            全市場交付計畫載入失敗：{query.error.message}
          </AlertDescription>
        </Alert>
      ) : (
        <>
          {query.isFetching && (
            <p role="status" className="text-xs text-muted">
              正在更新全市場交付計畫…
            </p>
          )}
          {query.data.data.length === 0 ? (
            <p className="text-sm text-muted">此條件沒有交付計畫。</p>
          ) : (
            <div className="space-y-3">
              {query.data.data.map(plan => (
                <PlanCard key={plan.plan_id} plan={plan} />
              ))}
            </div>
          )}
        </>
      )}
    </Panel>
  )
}
