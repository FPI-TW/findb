import type { ReactNode } from "react"
import { Skeleton } from "./ui/skeleton"
import { Button } from "./ui/button"

export type AsyncState = {
  hasData: boolean
  pending: boolean
  error?: unknown
  updatedAt?: number
}

export function asyncPhase(state: AsyncState) {
  if (!state.hasData) return state.error ? "error" : "initial-loading"
  if (state.error) return "stale-error"
  return state.pending ? "refreshing" : "ready"
}

function errorText(error: unknown) {
  return error instanceof Error ? error.message : String(error)
}

/** A single fixed-height status slot; refetches never insert content above a table. */
export function QueryStatus({
  hasData,
  pending,
  error,
  updatedAt,
  onRetry,
  label = "資料",
}: AsyncState & {
  onRetry?: () => void
  label?: string
}) {
  const phase = asyncPhase({ hasData, pending, error })
  const message = error
    ? `${label}更新失敗${hasData ? "，保留最後成功資料" : ""}：${errorText(error)}`
    : pending
      ? phase === "refreshing"
        ? `正在更新${label}…`
        : `正在載入${label}…`
      : ""
  const time = updatedAt
    ? new Intl.DateTimeFormat("zh-TW", {
        timeZone: "Asia/Taipei",
        dateStyle: "short",
        timeStyle: "medium",
      }).format(updatedAt)
    : ""
  return (
    <div
      className="mb-2 flex h-7 min-w-0 items-center gap-2 text-xs text-muted"
      data-slot="query-status"
    >
      <span
        className={
          error
            ? "min-w-0 flex-1 truncate text-danger"
            : "min-w-0 flex-1 truncate"
        }
        role={error ? "alert" : "status"}
        aria-live="polite"
        title={[message, time && `最後成功：${time}`]
          .filter(Boolean)
          .join(" · ")}
      >
        {message || (time ? `${label} · 最後成功：${time}` : "")}
        {message && time && <span className="ml-2">最後成功：{time}</span>}
      </span>
      {Boolean(error) && onRetry && (
        <Button
          className="h-6 shrink-0 px-2 text-xs"
          variant="outline"
          type="button"
          disabled={pending}
          onClick={onRetry}
        >
          重試
        </Button>
      )}
    </div>
  )
}

export function ContentSkeleton({
  label = "正在載入資料",
  children,
}: {
  label?: string
  children?: ReactNode
}) {
  return (
    <div
      role="status"
      aria-live="polite"
      className="grid min-h-36 gap-3 rounded-lg bg-surface-soft p-4"
    >
      <span className="sr-only">{label}</span>
      {children ?? (
        <>
          <Skeleton className="h-4 w-2/5" />
          <Skeleton className="h-4 w-full" />
          <Skeleton className="h-4 w-3/4" />
        </>
      )}
    </div>
  )
}
