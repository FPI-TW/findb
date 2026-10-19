import { QueryStatus } from "../../components/AsyncState"
import { useDeliveryResource } from "./delivery.queries"

const waitLabels: Record<string, string> = {
  recovery_required: "等待確認舊程序已停止並復原原工作",
  provider_active: "等待同供應商目前工作完成",
  not_before: "等待市場收盤或重試時間",
  suspended: "排程已暫停；讓其他可執行工作先行",
  prepared_drain: "等待排空原交付",
}

export function ProviderWorkPanel({ active = true }: { active?: boolean }) {
  const query = useDeliveryResource(
    { resource: "providerWork", page: 1, pageSize: 100 },
    active
  )
  const result =
    query.data?.resource === "providerWork" ? query.data.result : undefined
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
      ) : result?.data.length === 0 ? (
        <p className="text-sm text-muted">目前沒有等待或執行中的工作。</p>
      ) : (
        result && (
          <div className="overflow-x-auto">
            <table className="w-full text-left text-xs">
              <thead>
                <tr className="border-b border-line text-muted">
                  <th className="p-2">供應商 / 資料集</th>
                  <th className="p-2">來源 / 日期</th>
                  <th className="p-2">狀態 / 順位</th>
                  <th className="p-2">等待原因</th>
                  <th className="p-2">重試 / 最早時間</th>
                  <th className="p-2">心跳</th>
                </tr>
              </thead>
              <tbody>
                {result.data.map(work => (
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
                        <div className="font-medium text-ink">僅排空原交付</div>
                      )}
                      <div className="text-muted">
                        {work.position === null
                          ? "—"
                          : `第 ${work.position} 位`}
                      </div>
                    </td>
                    <td className="p-2">
                      {work.wait_reason
                        ? (waitLabels[work.wait_reason] ?? work.wait_reason)
                        : "—"}
                    </td>
                    <td className="p-2">
                      第 {work.attempt_number} 輪
                      <div className="text-muted">{work.not_before}</div>
                    </td>
                    <td className="p-2">{work.heartbeat_at ?? "—"}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )
      )}
    </section>
  )
}
