import { useState } from "react"
import { OperationsTabs } from "../../components/OperationsTabs"
import { SOURCE_PROVIDERS } from "../../lib/admin-governance-api"
import { QueryStatus } from "../../components/AsyncState"
import { useDeliveryResource } from "./delivery.queries"

const waitLabels: Record<string, string> = {
  recovery_required: "等待確認舊程序已停止並復原原工作",
  provider_active: "等待同供應商目前工作完成",
  not_before: "等待市場收盤或重試時間",
  suspended: "排程已暫停；讓其他可執行工作先行",
  prepared_drain: "等待排空原交付",
  account_cooldown: "等待共用帳號配額或冷卻",
  acquisition_window_closed: "每日抓取窗口外",
  batch_yield: "等待下一批輪替",
}

export function ProviderWorkPanel({ active = true }: { active?: boolean }) {
  const [selectedProvider, setSelectedProvider] = useState<string>(
    SOURCE_PROVIDERS[0]
  )
  const query = useDeliveryResource(
    { resource: "providerWork", page: 1, pageSize: 100 },
    active
  )
  const result =
    query.data?.resource === "providerWork" ? query.data.result : undefined
  const providers = Array.from(
    new Set([
      ...SOURCE_PROVIDERS,
      ...(result?.data.map(work => work.provider) ?? []),
    ])
  )
  const selected = providers.includes(selectedProvider)
    ? selectedProvider
    : SOURCE_PROVIDERS[0]
  return (
    <section
      className="mb-6 rounded-xl border border-line bg-surface p-4"
      aria-label="供應商工作佇列"
    >
      <h2 className="m-0 text-base font-bold text-ink">供應商工作佇列</h2>
      <p className="mt-1 text-xs text-muted">
        Pilot、Full market、歷史回補與維護共用同供應商
        FIFO；不同供應商可同時執行。
      </p>
      <QueryStatus
        hasData={result !== undefined}
        pending={query.isFetching}
        error={query.error}
        updatedAt={query.dataUpdatedAt}
        label="供應商佇列"
        onRetry={() => void query.refetch({ cancelRefetch: false })}
      />
      {result === undefined && !query.error ? (
        <div
          role="status"
          aria-live="polite"
          className="h-16 animate-pulse rounded bg-surface-soft p-4 text-sm text-muted"
        >
          載入供應商佇列…
        </div>
      ) : (
        result && (
          <OperationsTabs
            label="供應商工作佇列分頁"
            tabs={providers.map(provider => ({
              value: provider,
              label: `${provider} (${result.data.filter(work => work.provider === provider).length})`,
            }))}
            selected={selected}
            onChange={setSelectedProvider}
          >
            {provider => {
              if (provider !== selected) return null
              const workItems = result.data.filter(
                work => work.provider === provider
              )
              return workItems.length === 0 ? (
                <p className="text-sm text-muted">
                  此供應商目前沒有等待或執行中的工作。
                </p>
              ) : (
                <div className="overflow-x-auto">
                  <table
                    aria-label={`${provider} 工作佇列`}
                    className="w-full text-left text-xs"
                  >
                    <thead>
                      <tr className="border-b border-line text-muted">
                        <th className="p-2">供應商 / 資料集</th>
                        <th className="p-2">來源 / 日期</th>
                        <th className="p-2">狀態 / 順位</th>
                        <th className="p-2">等待原因</th>
                        <th className="p-2">輪替 / 最早時間</th>
                        <th className="p-2">心跳</th>
                      </tr>
                    </thead>
                    <tbody>
                      {workItems.map(work => (
                        <tr
                          key={work.work_id}
                          className="border-b border-line last:border-0"
                        >
                          <td className="p-2 font-medium">
                            {work.provider}
                            <div className="text-muted">{work.dataset_key}</div>
                          </td>
                          <td className="p-2">
                            {work.execution_profile ?? work.kind}
                            <div className="text-muted">
                              {work.trade_date ?? "維護"}
                            </div>
                          </td>
                          <td className="p-2">
                            {work.active ? "執行中 · " : ""}
                            {work.state}
                            {work.drain_only && (
                              <div className="font-medium text-ink">
                                僅排空原交付
                              </div>
                            )}
                            <div className="text-muted">
                              {work.position === null
                                ? "—"
                                : `第 ${work.position} 位`}
                            </div>
                          </td>
                          <td className="p-2">
                            {work.wait_reason
                              ? (waitLabels[work.wait_reason] ??
                                work.wait_reason)
                              : "—"}
                          </td>
                          <td className="p-2">
                            第 {work.attempt_number} 輪
                            <div className="text-muted">{work.not_before}</div>
                            {work.last_batch?.processed !== undefined && (
                              <div>
                                最近批次：{work.last_batch.processed} 個成員 ·{" "}
                                {work.last_batch.elapsed_seconds ?? 0} 秒
                              </div>
                            )}
                            {work.last_batch?.counts && (
                              <div className="text-muted">
                                完成 {work.last_batch.counts.complete ?? 0} ·
                                待處理{" "}
                                {(work.last_batch.counts.pending ?? 0) +
                                  (work.last_batch.counts.prepared ?? 0)}{" "}
                                · 人工處理 {work.last_batch.counts.manual ?? 0}
                              </div>
                            )}
                            {work.daily_runtime_window?.next_open_at && (
                              <div>
                                下次開窗：
                                {work.daily_runtime_window.next_open_at}
                              </div>
                            )}
                            {work.last_failure_code && (
                              <div>最近實際失敗：{work.last_failure_code}</div>
                            )}
                          </td>
                          <td className="p-2">{work.heartbeat_at ?? "—"}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )
            }}
          </OperationsTabs>
        )
      )}
    </section>
  )
}
