import { DATASET_MARKETS } from "../../lib/generated/provider-catalog"
import { useQueryClient } from "@tanstack/react-query"
import { useServerFn } from "@tanstack/react-start"
import {
  AlertTriangle,
  CalendarClock,
  CircleCheck,
  Database,
  Power,
  TriangleAlert,
} from "lucide-react"
import { useState } from "react"

import { useProtectedQueryScope } from "../../components/ProtectedQueryScope"
import { Alert, AlertDescription, AlertTitle } from "../../components/ui/alert"
import { Badge } from "../../components/ui/badge"
import { Button } from "../../components/ui/button"
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from "../../components/ui/alert-dialog"
import { Card } from "../../components/ui/card"
import { toast } from "../../components/ui/toast"
import type {
  DashboardRequest,
  MarketFreshness,
  MarketFreshnessResponse,
  PanelResult,
  Scheduler,
  SchedulerDesiredState,
  SchedulerMutationResponse,
  SchedulersResponse,
} from "../../lib/admin-api"
import type { AdminRole } from "../../lib/admin-governance-api"
import { updateScheduler } from "../../lib/admin.functions"
import { useOperationsRefresh } from "../../components/OperationsRefresh"
import {
  FeedDetails,
  formatAge,
  formatDate,
  formatScheduledTime,
  PageIntro,
  Panel,
  DateWithRelative,
  EmptyState,
  Metric,
  freshnessVariant,
  STATUS_LABELS,
  type OperationsDashboardQuery,
  OperationsDashboardStatus,
} from "./operations.shared"
import {
  updateOverviewSchedulerCache,
  useOperationsDashboardQuery,
  useOperationsDashboardState,
} from "./operations.queries"
import { OPERATIONS_OVERVIEW_AUDIT } from "./operations.search"

type SchedulerCardControl = Pick<
  Scheduler,
  | "feed_schedules"
  | "scheduler_key"
  | "provider"
  | "dataset_keys"
  | "slot_id"
  | "scheduled_local_time"
  | "timezone"
  | "desired_state"
  | "observed_state"
  | "revision"
  | "last_heartbeat_at"
  | "last_cycle_started_at"
  | "last_cycle_completed_at"
  | "last_error"
  | "heartbeat_age_seconds"
  | "start_allowed"
  | "start_blockers"
  | "acquisition_allowed"
  | "acquisition_blockers"
  | "expected_cycle_interval_seconds"
  | "running_since"
  | "environment"
  | "admitted_dataset_keys"
  | "first_start_dates"
  | "feed_readiness"
>

type IngestionCard = {
  control: SchedulerCardControl
  freshness: MarketFreshness | null
}

function cardMarkets(card: IngestionCard): string[] {
  const markets = card.control.feed_schedules?.map(feed => feed.market) ?? []
  const values = markets.length
    ? markets
    : card.control.dataset_keys.map(
        key => DATASET_MARKETS[key] ?? card.freshness?.market ?? "其他"
      )
  return [
    ...new Set(values.length ? values : [card.freshness?.market ?? "其他"]),
  ]
}

function controlFromFreshness(summary: MarketFreshness): SchedulerCardControl {
  return {
    feed_schedules: summary.feeds.flatMap(feed =>
      feed.schedule ? [feed.schedule] : []
    ),
    scheduler_key: summary.scheduler_key,
    provider: summary.provider || summary.feeds[0]?.source || "—",
    dataset_keys: summary.dataset_keys,
    slot_id: summary.slot_id,
    scheduled_local_time: summary.scheduled_local_time,
    timezone: summary.timezone,
    desired_state: summary.desired_state,
    observed_state: summary.observed_state,
    revision: summary.revision,
    last_heartbeat_at: summary.last_heartbeat_at,
    last_cycle_started_at: summary.last_cycle_started_at,
    last_cycle_completed_at: summary.last_cycle_completed_at,
    last_error: summary.last_error,
    heartbeat_age_seconds: summary.heartbeat_age_seconds,
  }
}

function buildIngestionCards(
  freshnessResult: PanelResult<MarketFreshnessResponse> | null,
  schedulersResult: PanelResult<SchedulersResponse> | null
): IngestionCard[] {
  const freshnessRows = freshnessResult?.ok ? freshnessResult.data.data : []
  const schedulerRows = schedulersResult?.ok ? schedulersResult.data.data : []
  const schedulerByKey = new Map(
    schedulerRows.map(row => [row.scheduler_key, row] as const)
  )
  const cards = new Map<string, IngestionCard>()
  freshnessRows.forEach(summary => {
    const control =
      schedulerByKey.get(summary.scheduler_key) ?? controlFromFreshness(summary)
    cards.set(control.scheduler_key, { control, freshness: summary })
  })
  schedulerRows.forEach(control => {
    if (!cards.has(control.scheduler_key)) {
      cards.set(control.scheduler_key, { control, freshness: null })
    }
  })
  return [...cards.values()].sort((left, right) =>
    left.control.scheduler_key.localeCompare(right.control.scheduler_key)
  )
}

/** Used when an older Backend omits `expected_cycle_interval_seconds`. */
export const DEFAULT_SCHEDULER_CYCLE_INTERVAL_SECONDS = 30
/** Heartbeat and cycle freshness tolerate three missed poll intervals. */
const SCHEDULER_INTERVAL_TOLERANCE = 3

export type SchedulerRuntimeStatus =
  | "configuration_error"
  | "not_activated"
  | "deactivated"
  | "not_reported"
  | "stale"
  | "stopping"
  | "stopped"
  | "acquiring"
  | "idle"
  | "starting"
  | "acquisition_blocked"
  | "cycle_failed"
  | "not_cycling"
  | "unknown"

type SchedulerRuntimeStatusMeta = {
  label: string
  variant: "default" | "secondary" | "warning" | "destructive"
}

export const SCHEDULER_RUNTIME_STATUS_META = {
  configuration_error: { label: "設定錯誤", variant: "destructive" },
  not_activated: { label: "尚未啟用", variant: "secondary" },
  deactivated: { label: "已停用", variant: "secondary" },
  not_reported: { label: "尚未回報", variant: "warning" },
  stale: { label: "心跳過期", variant: "warning" },
  stopping: { label: "停止中", variant: "warning" },
  stopped: { label: "已停止", variant: "secondary" },
  acquiring: { label: "抓取中", variant: "default" },
  idle: { label: "執行中：等待下一輪", variant: "default" },
  starting: { label: "啟動中：等待第一輪 cycle", variant: "secondary" },
  acquisition_blocked: { label: "已啟用但抓取被擋", variant: "warning" },
  cycle_failed: { label: "已啟用但上一輪未完成", variant: "warning" },
  not_cycling: { label: "已啟用但沒有進入 cycle", variant: "warning" },
  unknown: { label: "未知錯誤", variant: "destructive" },
} satisfies Record<SchedulerRuntimeStatus, SchedulerRuntimeStatusMeta>

export const OWNER_SETTING_LABELS: Record<Scheduler["desired_state"], string> =
  {
    running: "已啟用",
    stopped: "已停止",
  }

type SchedulerRuntimeControl = Pick<
  SchedulerCardControl,
  | "desired_state"
  | "observed_state"
  | "heartbeat_age_seconds"
  | "last_heartbeat_at"
  | "last_cycle_started_at"
  | "last_cycle_completed_at"
  | "revision"
> &
  Partial<
    Pick<
      SchedulerCardControl,
      | "acquisition_allowed"
      | "acquisition_blockers"
      | "expected_cycle_interval_seconds"
      | "running_since"
      | "last_error"
      | "provider"
    >
  >

type SchedulerRuntimeFreshness =
  | (Pick<MarketFreshness, "configuration_status" | "configuration_errors"> &
      Partial<
        Pick<MarketFreshness, "monitor_kind" | "activation_state" | "revision">
      >)
  | null

export function schedulerCycleIntervalSeconds(
  control: Pick<SchedulerRuntimeControl, "expected_cycle_interval_seconds">
) {
  const value = control.expected_cycle_interval_seconds
  return typeof value === "number" && Number.isFinite(value) && value > 0
    ? value
    : DEFAULT_SCHEDULER_CYCLE_INTERVAL_SECONDS
}

function schedulerStaleAfterSeconds(control: SchedulerRuntimeControl) {
  return SCHEDULER_INTERVAL_TOLERANCE * schedulerCycleIntervalSeconds(control)
}

/**
 * Seconds elapsed since `value` on the Backend clock. The Backend computes the
 * heartbeat age at response time, so this avoids trusting the browser clock.
 */
function secondsSince(
  control: SchedulerRuntimeControl,
  value: string | null | undefined
) {
  if (
    !value ||
    !control.last_heartbeat_at ||
    control.heartbeat_age_seconds === null ||
    !Number.isFinite(control.heartbeat_age_seconds)
  ) {
    return null
  }
  const offset =
    (Date.parse(control.last_heartbeat_at) - Date.parse(value)) / 1000
  if (!Number.isFinite(offset)) return null
  return Math.max(0, control.heartbeat_age_seconds + offset)
}

function cycleCompletedAfterStart(
  control: Pick<
    SchedulerRuntimeControl,
    "last_cycle_started_at" | "last_cycle_completed_at"
  >
) {
  const started = control.last_cycle_started_at
  const completed = control.last_cycle_completed_at
  return Boolean(
    started && completed && Date.parse(completed) >= Date.parse(started)
  )
}

export function selectSchedulerRuntimeStatus({
  control,
  freshness,
}: {
  control: SchedulerRuntimeControl
  freshness: SchedulerRuntimeFreshness
}): SchedulerRuntimeStatus {
  if (
    freshness?.configuration_status === "error" ||
    (freshness?.configuration_errors.length ?? 0) > 0 ||
    (freshness?.monitor_kind === "full_market" &&
      control.desired_state === "running" &&
      // Activation computed for an older revision predates this start's
      // admission; wait for the refetch instead of reporting an error.
      (freshness.revision ?? control.revision) === control.revision &&
      (freshness.activation_state === "not_activated" ||
        freshness.activation_state === "deactivated"))
  ) {
    return "configuration_error"
  }
  if (
    freshness?.monitor_kind === "full_market" &&
    control.desired_state === "stopped" &&
    control.observed_state === "stopped"
  ) {
    if (freshness.activation_state === "not_activated") return "not_activated"
    if (freshness.activation_state === "deactivated") return "deactivated"
    return "stopped"
  }
  if (!control.last_heartbeat_at || control.heartbeat_age_seconds === null) {
    return "not_reported"
  }
  if (!Number.isFinite(control.heartbeat_age_seconds)) return "unknown"
  const staleAfter = schedulerStaleAfterSeconds(control)
  if (control.heartbeat_age_seconds > staleAfter) return "stale"
  if (control.desired_state === "stopped") {
    if (control.observed_state === "running") return "stopping"
    if (control.observed_state === "stopped") return "stopped"
    return "unknown"
  }
  if (control.desired_state !== "running") return "unknown"
  if (control.acquisition_allowed === false) return "acquisition_blocked"
  const started = control.last_cycle_started_at
  const completed = cycleCompletedAfterStart(control)
  // observed=running between cycles only means the runtime is re-checking
  // authorization; a cycle is in progress only while it has not completed.
  if (control.observed_state === "running" && started && !completed) {
    return "acquiring"
  }
  const startedAge = secondsSince(control, started)
  if (startedAge !== null && startedAge <= staleAfter) {
    return completed ? "idle" : "cycle_failed"
  }
  const runningSince = control.running_since
  const sinceStart = secondsSince(control, runningSince)
  if (
    runningSince &&
    sinceStart !== null &&
    sinceStart <= staleAfter &&
    (!started || Date.parse(started) < Date.parse(runningSince))
  ) {
    return "starting"
  }
  return "not_cycling"
}

const UNKNOWN_BLOCKER_REASON = "全市場設定或 scope 無效"

const SCHEDULER_BLOCKER_REASONS: Record<string, string> = {
  full_market_no_enabled_datasets: "尚未啟用任何全市場 feed",
  full_market_runtime_missing: "尚未安裝並登錄 runtime",
  full_market_readiness_not_reported: "已登錄 runtime 尚未回報 readiness",
  full_market_readiness_expired: "readiness 證據已過期",
  full_market_no_ready_feeds: "沒有 Ready feed",
  full_market_request_capacity_insufficient: "provider 每日 request 容量不足",
  full_market_byte_capacity_insufficient: "provider 每日資料量容量不足",
  full_market_deadline_capacity_insufficient: "provider 無法在 deadline 完成",
  full_market_configuration_invalid:
    "全市場設定、readiness 或 scope 不符合啟動條件",
  full_market_baseline_missing: "缺少已發布的官方 baseline",
  provider_acquisition_cutover: "供應商來源切換限制，目前不允許此供應商抓取",
  full_market_calendar_missing: "缺少完整已發布的交易所日曆",
  full_market_source_binding_invalid:
    "Source credential 的供應商或資料集綁定無效",
  daily_acquisition_window_closed: "目前不在每日抓取窗口內",
}

function schedulerBlockerReason(code: string, provider: string | undefined) {
  if (provider === "factset" && code === "full_market_baseline_missing") {
    return "尚無已發布的 FactSet HKEX baseline"
  }
  if (provider === "factset" && code === "provider_acquisition_cutover") {
    return "尚未提交 HK provider 切換"
  }
  return SCHEDULER_BLOCKER_REASONS[code]
}

export type SchedulerRuntimeSummary = SchedulerRuntimeStatusMeta & {
  status: SchedulerRuntimeStatus
  explanation: string
  blockers: string[]
}

function formatDuration(seconds: number | null) {
  return seconds === null ? "時間未知" : formatAge(seconds)
}

export function describeSchedulerRuntime({
  control,
  freshness,
}: {
  control: SchedulerRuntimeControl
  freshness: SchedulerRuntimeFreshness
}): SchedulerRuntimeSummary {
  const status = selectSchedulerRuntimeStatus({ control, freshness })
  const interval = schedulerCycleIntervalSeconds(control)
  const staleAfter = schedulerStaleAfterSeconds(control)
  const cycleAge = formatDuration(
    secondsSince(control, control.last_cycle_started_at)
  )
  const blockers =
    status === "acquisition_blocked" || status === "not_cycling"
      ? (control.acquisition_blockers ?? []).map(
          code =>
            schedulerBlockerReason(code, control.provider) ??
            `${UNKNOWN_BLOCKER_REASON}（${code}）`
        )
      : []
  const explanations: Record<SchedulerRuntimeStatus, string> = {
    configuration_error:
      "全市場設定或啟用範圍有誤；修正下方設定錯誤前不會正常抓取。",
    not_activated: "尚未凍結任何啟用範圍；Owner 啟用後 runtime 才會開始抓取。",
    deactivated: "此排程的啟用範圍已停用；不會開始新的抓取。",
    not_reported:
      "Fetcher runtime 尚未回報心跳；請確認 runtime 已部署並正在執行。",
    stale: `心跳已 ${formatAge(control.heartbeat_age_seconds)}未更新，超過 ${staleAfter} 秒（3 × poll 間隔 ${interval} 秒）；runtime 可能已停止或與 Source 失聯。`,
    stopped: "Owner 已停止，runtime 也沒有進行中的 cycle；不會再開始新的抓取。",
    stopping: `Owner 已停止，runtime 仍在完成本輪 cycle（已執行 ${cycleAge}）；本輪結束後才會回報已停止。`,
    acquiring: `本輪 cycle 已執行 ${cycleAge}；runtime 只在 cycle 期間回報 observed=running。`,
    idle: `上一輪 ${formatDate(control.last_cycle_completed_at)} 完成；cycle 之間回報 observed=stopped 是正常閒置，約每 ${interval} 秒開始下一輪。`,
    starting: `Owner 已於 ${formatDuration(secondsSince(control, control.running_since))}前啟用；runtime 會在下一次 poll（約 ${interval} 秒內）開始第一輪 cycle。`,
    acquisition_blocked: blockers.length
      ? "Source 即時授權拒絕新抓取；排除下列原因前 runtime 不會開始 cycle。"
      : "Source 即時授權拒絕新抓取，未提供原因；請查看「詳細」中的 feed readiness。",
    cycle_failed: `最近一輪 cycle 開始後沒有正常完成${control.last_error ? `（${control.last_error}）` : ""}；runtime 會在下一輪重試，持續發生請查 runtime 日誌。`,
    not_cycling: `Owner 已啟用，但超過 ${staleAfter} 秒沒有開始新的 cycle；請確認 Source 授權、runtime 日誌與最近錯誤。`,
    unknown: "Runtime 回報的狀態無法判讀；請查 Admin API 原始欄位。",
  }
  return {
    status,
    ...SCHEDULER_RUNTIME_STATUS_META[status],
    explanation: explanations[status],
    blockers,
  }
}

/** The single wording for every scheduler activity count on the page. */
export function schedulerActivityLabel(
  rows: {
    desired_state: Scheduler["desired_state"]
    status: SchedulerRuntimeStatus
  }[]
) {
  const enabled = rows.filter(row => row.desired_state === "running").length
  const acquiring = rows.filter(row => row.status === "acquiring").length
  return `${enabled} 已啟用 · ${acquiring} 抓取中`
}

function schedulerErrorMessage(reason: unknown) {
  if (typeof reason === "object" && reason !== null && "status" in reason) {
    if ((reason as { status?: unknown }).status === 409) {
      return "排程版本已被其他使用者更新，請先重新整理後再試。"
    }
  }
  if (reason instanceof Error) {
    if (
      reason.message.toLowerCase().includes("revision") ||
      reason.message.includes("版本") ||
      reason.message.includes("409")
    ) {
      return "排程版本已被其他使用者更新，請先重新整理後再試。"
    }
    return reason.message
  }
  return "排程狀態更新失敗，請稍後再試。"
}

type SchedulerMutationTarget = Pick<
  Scheduler,
  | "scheduler_key"
  | "provider"
  | "desired_state"
  | "observed_state"
  | "revision"
  | "start_allowed"
  | "start_blockers"
>

function schedulerStartAllowed(control: SchedulerMutationTarget) {
  return control.start_allowed ?? false
}

function schedulerStartReason(control: SchedulerMutationTarget) {
  return control.start_blockers?.length
    ? control.start_blockers
        .map(
          code =>
            schedulerBlockerReason(code, control.provider) ??
            UNKNOWN_BLOCKER_REASON
        )
        .join("；")
    : "缺少全市場啟動資格，請重新整理並確認治理設定"
}

type SchedulerBulkConfirmation = {
  desiredState: SchedulerDesiredState
  targets: SchedulerMutationTarget[]
  skipped: SchedulerMutationTarget[]
}

function SchedulerConfirmationDialog({
  target,
  onCancel,
  onConfirm,
}: {
  target: SchedulerMutationTarget | null
  onCancel: () => void
  onConfirm: () => void
}) {
  const desiredState =
    target?.desired_state === "running" ? "stopped" : "running"
  const actionLabel = desiredState === "running" ? "啟用" : "停止"
  return (
    <AlertDialog
      open={target !== null}
      onOpenChange={open => {
        if (!open) onCancel()
      }}
    >
      {target && (
        <AlertDialogContent className="max-h-[calc(100dvh-2rem)] overflow-y-auto">
          <AlertDialogHeader>
            <AlertDialogTitle>確認{actionLabel}排程？</AlertDialogTitle>
            <AlertDialogDescription>
              確定要{actionLabel} {target.provider} 的排程「
              {target.scheduler_key}」嗎？
              {desiredState === "running"
                ? "啟用後系統會依照設定時間執行資料抓取。"
                : "停止後系統將不再依排程自動抓取資料。"}
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel onClick={onCancel}>取消</AlertDialogCancel>
            <AlertDialogAction
              variant={desiredState === "running" ? "default" : "destructive"}
              onClick={onConfirm}
            >
              確認{actionLabel}
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      )}
    </AlertDialog>
  )
}

function SchedulerBulkConfirmationDialog({
  confirmation,
  onCancel,
  onConfirm,
}: {
  confirmation: SchedulerBulkConfirmation | null
  onCancel: () => void
  onConfirm: () => void
}) {
  const actionLabel =
    confirmation?.desiredState === "running" ? "全部啟動" : "全部停止"
  return (
    <AlertDialog
      open={confirmation !== null}
      onOpenChange={open => {
        if (!open) onCancel()
      }}
    >
      {confirmation && (
        <AlertDialogContent className="max-h-[calc(100dvh-2rem)] overflow-y-auto">
          <AlertDialogHeader>
            <AlertDialogTitle>確認{actionLabel}排程？</AlertDialogTitle>
            <AlertDialogDescription>
              將依序更新 {confirmation.targets.length} 個
              Scheduler，且每筆都會以目前 revision 防止覆蓋其他人剛完成的操作。
              {confirmation.desiredState === "stopped"
                ? "送出後仍須等待每張卡片的「Owner 設定」與「Runtime 實況」都顯示為已停止，才能開始部署。"
                : "送出後請逐一確認每張卡片的「Runtime 實況」進入執行中或抓取中，部分失敗不會自動重試。"}
            </AlertDialogDescription>
          </AlertDialogHeader>
          <ul className="my-0 max-h-48 overflow-auto pl-5 font-mono text-xs text-muted">
            {confirmation.targets.map(target => (
              <li key={target.scheduler_key}>
                {target.provider} / {target.scheduler_key} / r{target.revision}
              </li>
            ))}
          </ul>
          {confirmation.skipped.length > 0 && (
            <p className="text-xs text-muted">
              跳過 {confirmation.skipped.length} 個無法啟動的 Scheduler：
              {confirmation.skipped
                .map(
                  target =>
                    `${target.scheduler_key}（${schedulerStartReason(target)}）`
                )
                .join("；")}
            </p>
          )}
          <AlertDialogFooter>
            <AlertDialogCancel onClick={onCancel}>取消</AlertDialogCancel>
            <AlertDialogAction
              variant={
                confirmation.desiredState === "running"
                  ? "default"
                  : "destructive"
              }
              onClick={onConfirm}
            >
              確認{actionLabel}
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      )}
    </AlertDialog>
  )
}

function useSchedulerActions(
  audit: DashboardRequest["audit"] = { ...OPERATIONS_OVERVIEW_AUDIT },
  applyScheduler?: (response: SchedulerMutationResponse) => void
) {
  const update = useServerFn(updateScheduler)
  const { reportError } = useOperationsRefresh()
  const queryClient = useQueryClient()
  const sessionScope = useProtectedQueryScope()
  const [pendingKeys, setPendingKeys] = useState<Set<string>>(() => new Set())
  const [actionErrors, setActionErrors] = useState<Record<string, string>>({})
  const [confirmationTarget, setConfirmationTarget] =
    useState<SchedulerMutationTarget | null>(null)
  const [bulkConfirmation, setBulkConfirmation] =
    useState<SchedulerBulkConfirmation | null>(null)
  const [bulkPending, setBulkPending] = useState(false)

  async function applyDesiredState(
    control: SchedulerMutationTarget,
    desiredState: SchedulerDesiredState
  ) {
    const response = await update({
      data: {
        schedulerKey: control.scheduler_key,
        desiredState,
        expectedRevision: control.revision,
      },
    })
    updateOverviewSchedulerCache(queryClient, audit, response, sessionScope)
    applyScheduler?.(response)
    return response
  }

  async function toggleScheduler(control: SchedulerMutationTarget) {
    if (bulkPending || pendingKeys.size > 0) return
    const desiredState: SchedulerDesiredState =
      control.desired_state === "running" ? "stopped" : "running"
    setPendingKeys(current => new Set(current).add(control.scheduler_key))
    setActionErrors(current => {
      const next = { ...current }
      delete next[control.scheduler_key]
      return next
    })
    try {
      await applyDesiredState(control, desiredState)
      toast.success(
        `${control.provider} 排程已${desiredState === "running" ? "啟用" : "停止"}`
      )
    } catch (reason) {
      if (reportError(reason)) return
      const message = schedulerErrorMessage(reason)
      setActionErrors(current => ({
        ...current,
        [control.scheduler_key]: message,
      }))
      toast.error("排程狀態更新失敗", { description: message })
    } finally {
      setPendingKeys(current => {
        const next = new Set(current)
        next.delete(control.scheduler_key)
        return next
      })
    }
  }

  async function updateSchedulersInSequence(
    targets: SchedulerMutationTarget[],
    desiredState: SchedulerDesiredState
  ) {
    if (bulkPending || pendingKeys.size > 0 || targets.length === 0) return
    setBulkPending(true)
    setPendingKeys(current => {
      const next = new Set(current)
      for (const target of targets) next.add(target.scheduler_key)
      return next
    })
    setActionErrors(current => {
      const next = { ...current }
      for (const target of targets) delete next[target.scheduler_key]
      return next
    })

    let succeeded = 0
    let failed = 0
    let authenticationFailed = false
    try {
      for (const target of targets) {
        try {
          await applyDesiredState(target, desiredState)
          succeeded += 1
        } catch (reason) {
          if (reportError(reason)) {
            authenticationFailed = true
            break
          }
          failed += 1
          const message = schedulerErrorMessage(reason)
          setActionErrors(current => ({
            ...current,
            [target.scheduler_key]: message,
          }))
        }
      }

      if (!authenticationFailed && failed === 0) {
        toast.success(
          `Scheduler 已全部${desiredState === "running" ? "啟動" : "停止"}`,
          {
            description: `成功更新 ${succeeded} 筆；請繼續確認各卡片的 Runtime 實況。`,
          }
        )
      } else if (!authenticationFailed) {
        toast.error("Scheduler 批次更新未完整完成", {
          description: `成功 ${succeeded} 筆，失敗 ${failed} 筆；請檢視各卡片錯誤後重試。`,
        })
      }
    } finally {
      setPendingKeys(current => {
        const next = new Set(current)
        for (const target of targets) next.delete(target.scheduler_key)
        return next
      })
      setBulkPending(false)
    }
  }

  return {
    pendingKeys,
    actionErrors,
    bulkPending,
    requestBulkState: (
      desiredState: SchedulerDesiredState,
      controls: SchedulerMutationTarget[]
    ) => {
      if (bulkPending || pendingKeys.size > 0) return
      const candidates = controls.filter(
        scheduler => scheduler.desired_state !== desiredState
      )
      const skipped =
        desiredState === "running"
          ? candidates.filter(scheduler => !schedulerStartAllowed(scheduler))
          : []
      const targets = candidates.filter(
        scheduler =>
          desiredState === "stopped" || schedulerStartAllowed(scheduler)
      )
      if (targets.length > 0)
        setBulkConfirmation({ desiredState, targets, skipped })
    },
    requestToggleScheduler: (control: SchedulerMutationTarget) => {
      if (
        !bulkPending &&
        pendingKeys.size === 0 &&
        (control.desired_state === "running" || schedulerStartAllowed(control))
      )
        setConfirmationTarget(control)
    },
    dialog: (
      <>
        <SchedulerConfirmationDialog
          target={confirmationTarget}
          onCancel={() => setConfirmationTarget(null)}
          onConfirm={() => {
            const target = confirmationTarget
            setConfirmationTarget(null)
            if (target) void toggleScheduler(target)
          }}
        />
        <SchedulerBulkConfirmationDialog
          confirmation={bulkConfirmation}
          onCancel={() => setBulkConfirmation(null)}
          onConfirm={() => {
            const confirmation = bulkConfirmation
            setBulkConfirmation(null)
            if (confirmation) {
              void updateSchedulersInSequence(
                confirmation.targets,
                confirmation.desiredState
              )
            }
          }}
        />
      </>
    ),
  }
}

type IngestionCardState = {
  card: IngestionCard
  runtime: SchedulerRuntimeSummary
}

function SchedulerDeploymentReadiness({
  states,
}: {
  states: IngestionCardState[]
}) {
  const pending = states.filter(
    ({ card }) =>
      card.control.desired_state !== "stopped" ||
      card.control.observed_state !== "stopped"
  )
  if (pending.length === 0) {
    return (
      <Alert variant="success" role="status" className="mt-2">
        <CircleCheck size={17} />
        <AlertDescription>
          <span className="font-semibold">可部署：</span>
          全部 {states.length} 個排程的「Owner 設定」與「Runtime
          實況」均為已停止。
        </AlertDescription>
      </Alert>
    )
  }
  return (
    <Alert variant="warning" role="status" className="mt-2">
      <TriangleAlert size={17} />
      <AlertDescription>
        <span className="font-semibold">
          尚不可部署：{pending.length} 個排程還沒停好
        </span>
        <ul className="m-0 list-disc pl-5">
          {pending.map(({ card, runtime }) => (
            <li key={card.control.scheduler_key}>
              <span className="font-mono">{card.control.scheduler_key}</span>
              ：Owner 設定 {OWNER_SETTING_LABELS[card.control.desired_state]}
              {" · "}Runtime 實況 {runtime.label}
            </li>
          ))}
        </ul>
      </AlertDescription>
    </Alert>
  )
}

function SchedulerBulkControls({
  role,
  schedulers,
  states,
  actions,
}: {
  role: AdminRole
  schedulers: SchedulerMutationTarget[]
  states: IngestionCardState[]
  actions: ReturnType<typeof useSchedulerActions>
}) {
  if (states.length === 0) return null
  const blocked = schedulers.filter(
    control =>
      control.desired_state !== "running" && !schedulerStartAllowed(control)
  )

  return (
    <div className="mb-4 grid gap-3 rounded-xl border border-line bg-surface p-4 sm:grid-cols-[1fr_auto] sm:items-start">
      <div>
        <p className="m-0 text-sm font-semibold">批次控制</p>
        <p className="mt-1 mb-0 text-xs text-muted">
          {schedulerActivityLabel(
            states.map(({ card, runtime }) => ({
              desired_state: card.control.desired_state,
              status: runtime.status,
            }))
          )}
          （共 {states.length} 個排程）
        </p>
        <p className="mt-1 mb-0 text-xs text-muted">
          停止後請等待所有卡片的「Owner 設定」與「Runtime 實況」均為已停止。
        </p>
        <SchedulerDeploymentReadiness states={states} />
        {blocked.length > 0 && (
          <p className="mt-2 mb-0 text-xs text-muted" role="note">
            全部啟動將跳過 {blocked.length} 個 Scheduler：
            {blocked
              .map(
                control =>
                  `${control.scheduler_key}（${schedulerStartReason(control)}）`
              )
              .join("；")}
          </p>
        )}
      </div>
      {role === "owner" && schedulers.length > 0 && (
        <div className="flex flex-wrap gap-2">
          <Button
            type="button"
            variant="destructive"
            disabled={
              actions.bulkPending ||
              actions.pendingKeys.size > 0 ||
              schedulers.every(
                scheduler => scheduler.desired_state === "stopped"
              )
            }
            aria-busy={actions.bulkPending}
            onClick={() => actions.requestBulkState("stopped", schedulers)}
          >
            <Power aria-hidden="true" />
            全部停止
          </Button>
          <Button
            type="button"
            disabled={
              actions.bulkPending ||
              actions.pendingKeys.size > 0 ||
              schedulers.every(
                scheduler =>
                  scheduler.desired_state === "running" ||
                  !schedulerStartAllowed(scheduler)
              )
            }
            aria-busy={actions.bulkPending}
            onClick={() => actions.requestBulkState("running", schedulers)}
          >
            <Power aria-hidden="true" />
            全部啟動
          </Button>
        </div>
      )}
    </div>
  )
}

function SchedulerActionButton({
  role,
  control,
  pending,
  requestToggle,
}: {
  role: AdminRole
  control: SchedulerMutationTarget
  pending: boolean
  requestToggle: (control: SchedulerMutationTarget) => void
}) {
  const nextState = control.desired_state === "running" ? "stopped" : "running"
  if (role !== "owner") {
    return (
      <span className="text-xs text-muted" role="note">
        唯讀：只有 owner 可以變更排程狀態。
      </span>
    )
  }
  return (
    <div className="grid gap-2">
      <Button
        type="button"
        variant={
          control.desired_state === "running" ? "destructive" : "default"
        }
        onClick={() => requestToggle(control)}
        disabled={
          pending ||
          (nextState === "running" && !schedulerStartAllowed(control))
        }
        aria-busy={pending}
        aria-label={`${control.provider} ${control.scheduler_key} 設為${OWNER_SETTING_LABELS[nextState]}`}
      >
        <Power aria-hidden="true" />
        {pending
          ? "更新中…"
          : nextState === "running"
            ? "啟用排程"
            : "停止排程"}
      </Button>
      {nextState === "running" && !schedulerStartAllowed(control) && (
        <span className="text-xs text-muted" role="note">
          無法啟動：{schedulerStartReason(control)}
        </span>
      )}
    </div>
  )
}

const recordCount = new Intl.NumberFormat("zh-TW")

function LastCycleSummary({ control }: { control: SchedulerCardControl }) {
  const started = control.last_cycle_started_at
  if (!started) return <>尚無 cycle 紀錄</>
  if (cycleCompletedAfterStart(control)) {
    return <>{formatDate(control.last_cycle_completed_at)} 正常完成</>
  }
  return control.observed_state === "running" ? (
    <>{formatDate(started)} 開始，進行中</>
  ) : (
    <>{formatDate(started)} 開始，未正常完成</>
  )
}

/** Current trade-date progress from the persisted plan summary in freshness. */
function CurrentDeliveryProgress({
  freshness,
}: {
  freshness: MarketFreshness
}) {
  if (freshness.feeds.length === 0) return <>尚無 feed</>
  return (
    <ul className="m-0 grid gap-0.5 p-0">
      {freshness.feeds.map(feed => (
        <li
          key={`${feed.dataset_key}:${feed.source}`}
          className="list-none text-xs"
        >
          <span className="font-mono">{feed.dataset_key}</span>
          {" · "}
          {!feed.expected_data_date
            ? "尚無應交付交易日"
            : feed.total_records === null
              ? `${feed.expected_data_date}：尚未建立交付計畫`
              : `${feed.expected_data_date}：data ${recordCount.format(feed.success_records ?? 0)} / ${recordCount.format(feed.total_records)}${feed.failed_records ? ` · blocked ${recordCount.format(feed.failed_records)}` : ""}`}
          {" · "}
          {STATUS_LABELS[feed.status]}
        </li>
      ))}
    </ul>
  )
}

function IngestionCardView({
  state,
  role,
  pending,
  actionError,
  requestToggle,
}: {
  state: IngestionCardState
  role: AdminRole
  pending: boolean
  actionError: string | undefined
  requestToggle: (control: SchedulerMutationTarget) => void
}) {
  const { card, runtime } = state
  const { control, freshness } = card
  const interval = schedulerCycleIntervalSeconds(control)
  const normalizationCompleted =
    freshness?.last_complete_at ?? freshness?.last_successful_update_at ?? null
  return (
    <article className="grid gap-4 rounded-xl border border-line bg-surface p-4">
      <div className="min-w-0">
        <h3 className="m-0 font-mono text-sm font-bold wrap-anywhere">
          {control.scheduler_key}
        </h3>
        <p className="mt-1 mb-0 text-xs text-muted">
          Provider：{control.provider} · Dataset：
          {control.dataset_keys.join(", ") || "—"}
        </p>
        {control.feed_schedules?.length ? (
          control.feed_schedules.map(feed => (
            <p key={feed.dataset_key} className="mt-1 mb-0 text-xs text-muted">
              {feed.dataset_key} · 每日{" "}
              {formatScheduledTime(feed.scheduled_time)} ·{" "}
              {feed.source_timezone}
            </p>
          ))
        ) : (
          <p className="mt-1 mb-0 text-xs text-muted">尚無發布排程</p>
        )}
      </div>
      <div className="grid gap-3 sm:grid-cols-2">
        <section
          aria-label={`${control.scheduler_key} Owner 設定`}
          className="grid content-start gap-1.5 rounded-lg border border-line bg-surface-soft p-3"
        >
          <h4 className="m-0 text-xs font-semibold text-muted">Owner 設定</h4>
          <div className="flex flex-wrap items-center gap-2">
            <Badge
              variant={
                control.desired_state === "running" ? "default" : "secondary"
              }
            >
              {OWNER_SETTING_LABELS[control.desired_state]}
            </Badge>
            <span className="font-mono text-xs text-muted">
              r{control.revision}
            </span>
          </div>
          <p className="m-0 text-xs text-muted">
            {control.desired_state === "running"
              ? "Owner 要求依排程持續抓取；實際有沒有在抓取請看 Runtime 實況。"
              : "Owner 已停止此排程；runtime 不會開始新的抓取。"}
          </p>
        </section>
        <section
          aria-label={`${control.scheduler_key} Runtime 實況`}
          className="grid content-start gap-1.5 rounded-lg border border-line bg-surface-soft p-3"
        >
          <h4 className="m-0 text-xs font-semibold text-muted">Runtime 實況</h4>
          <div>
            <Badge variant={runtime.variant}>{runtime.label}</Badge>
          </div>
          <p className="m-0 text-xs text-muted">{runtime.explanation}</p>
          {runtime.blockers.length > 0 && (
            <ul className="m-0 list-disc pl-5 text-xs text-warning">
              {runtime.blockers.map(reason => (
                <li key={reason}>{reason}</li>
              ))}
            </ul>
          )}
        </section>
      </div>
      <dl className="grid gap-3 text-sm sm:grid-cols-2">
        <div>
          <dt className="text-xs text-muted">最近一輪 cycle</dt>
          <dd className="mt-0.5">
            <LastCycleSummary control={control} />
          </dd>
        </div>
        {freshness?.monitor_kind === "full_market" && (
          <div>
            <dt className="text-xs text-muted">當期交付進度</dt>
            <dd className="mt-0.5">
              <CurrentDeliveryProgress freshness={freshness} />
            </dd>
          </div>
        )}
      </dl>
      {freshness?.configuration_errors.length ? (
        <Alert variant="warning" role="status">
          <TriangleAlert size={17} />
          <AlertDescription>
            <span className="font-semibold">設定錯誤：</span>
            {freshness.configuration_errors.slice(0, 4).join("；")}
            {freshness.configuration_errors.length > 4 && "；…"}
          </AlertDescription>
        </Alert>
      ) : null}
      {control.last_error && (
        <Alert variant="warning" role="status">
          <TriangleAlert size={17} />
          <AlertDescription>
            <span className="font-semibold">最近錯誤：</span>
            {control.last_error}
          </AlertDescription>
        </Alert>
      )}
      <details className="rounded-lg bg-surface-soft px-3 py-2">
        <summary className="cursor-pointer text-xs font-bold text-accent">
          詳細
        </summary>
        <div className="mt-3 grid gap-3">
          <dl className="grid gap-3 text-sm sm:grid-cols-2 lg:grid-cols-4">
            <div>
              <dt className="text-xs text-muted">Revision</dt>
              <dd className="mt-0.5 font-mono">r{control.revision}</dd>
            </div>
            <div>
              <dt className="text-xs text-muted">Heartbeat</dt>
              <dd className="mt-0.5">
                {formatAge(control.heartbeat_age_seconds)}
                <span className="mx-1 text-muted">·</span>
                <DateWithRelative value={control.last_heartbeat_at} />
              </dd>
            </div>
            <div>
              <dt className="text-xs text-muted">Poll 間隔</dt>
              <dd className="mt-0.5">
                {interval} 秒 · 心跳過期門檻{" "}
                {SCHEDULER_INTERVAL_TOLERANCE * interval} 秒
              </dd>
            </div>
            <div>
              <dt className="text-xs text-muted">Owner 啟用時間</dt>
              <dd className="mt-0.5">
                <DateWithRelative value={control.running_since ?? null} />
              </dd>
            </div>
            <div>
              <dt className="text-xs text-muted">最近 cycle 開始</dt>
              <dd className="mt-0.5">
                <DateWithRelative value={control.last_cycle_started_at} />
              </dd>
            </div>
            <div>
              <dt className="text-xs text-muted">最近 cycle 完成</dt>
              <dd className="mt-0.5">
                <DateWithRelative value={control.last_cycle_completed_at} />
              </dd>
            </div>
            <div>
              <dt className="text-xs text-muted">Provider fetched</dt>
              <dd className="mt-0.5">
                <DateWithRelative value={freshness?.last_fetched_at ?? null} />
              </dd>
            </div>
            <div>
              <dt className="text-xs text-muted">Normalization completed</dt>
              <dd className="mt-0.5">
                <DateWithRelative value={normalizationCompleted} />
              </dd>
            </div>
            {freshness && (
              <>
                <div>
                  <dt className="text-xs text-muted">Feed 完成</dt>
                  <dd className="mt-0.5 font-mono">
                    {freshness.fresh_feed_count} / {freshness.feed_count}
                    {freshness.late_feed_count > 0 &&
                      ` · ${freshness.late_feed_count} 延遲`}
                  </dd>
                </div>
                <div>
                  <dt className="text-xs text-muted">涵蓋日 / 預期日</dt>
                  <dd className="mt-0.5 font-mono">
                    {freshness.coverage_data_date ?? "—"} /{" "}
                    {freshness.expected_data_date ?? "—"}
                  </dd>
                </div>
              </>
            )}
          </dl>
          {control.scheduler_key.startsWith("full_market_") && (
            <div className="grid gap-2 text-xs text-muted">
              <p className="m-0">環境：{control.environment ?? "—"}</p>
              <p className="m-0">
                本次凍結範圍：
                {control.admitted_dataset_keys?.join(", ") || "無"}
              </p>
              {control.feed_readiness?.map(feed => (
                <p className="m-0" key={feed.dataset_key}>
                  {feed.dataset_key}：
                  {feed.ready ? "Ready" : feed.blockers.join("；")} · 首次日期{" "}
                  {control.first_start_dates?.[feed.dataset_key] ??
                    feed.first_start_date ??
                    "尚未啟動"}
                </p>
              ))}
            </div>
          )}
          {freshness?.monitor_kind === "full_market" && (
            <div className="grid gap-2 text-xs text-muted">
              <p className="m-0">
                已啟用範圍：
                {freshness.active_dataset_keys.join(", ") || "無"}
                。資料更新不代表此模式已啟用或排程已回報心跳。
              </p>
              {!control.last_heartbeat_at && (
                <p className="m-0">
                  排程尚未回報心跳；請確認部署與 Owner 啟用狀態。
                </p>
              )}
              {freshness.pending_feeds.length > 0 && (
                <div>
                  <strong>尚未納入抓取範圍：</strong>
                  <ul className="mt-1 mb-0 list-disc pl-5">
                    {freshness.pending_feeds.map(feed => (
                      <li key={feed.dataset_key}>
                        {feed.dataset_key}：
                        {feed.blockers.join("；") || "尚未啟用"}
                      </li>
                    ))}
                  </ul>
                </div>
              )}
            </div>
          )}
        </div>
      </details>
      {freshness && (
        <details className="rounded-lg bg-surface-soft px-3 py-2">
          <summary className="cursor-pointer text-xs font-bold text-accent">
            Feed 明細（{freshness.feeds.length}）
          </summary>
          <FeedDetails feeds={freshness.feeds} />
        </details>
      )}
      <div className="flex flex-wrap items-center justify-between gap-2 border-t border-line pt-3">
        <SchedulerActionButton
          role={role}
          control={control}
          pending={pending}
          requestToggle={requestToggle}
        />
        {actionError && (
          <p
            className="m-0 text-xs text-danger"
            role="alert"
            aria-live="polite"
          >
            {actionError}
          </p>
        )}
      </div>
    </article>
  )
}

export function IngestionOverviewPanel({
  freshnessResult,
  schedulersResult,
  loading,
  role,
  applyScheduler,
  audit = OPERATIONS_OVERVIEW_AUDIT,
}: {
  freshnessResult: PanelResult<MarketFreshnessResponse> | null
  schedulersResult: PanelResult<SchedulersResponse> | null
  loading: boolean
  pending: boolean
  freshnessError: string
  schedulersError: string
  role: AdminRole
  applyScheduler?: (response: SchedulerMutationResponse) => void
  audit?: DashboardRequest["audit"]
}) {
  const result: PanelResult<{ cards: IngestionCard[] }> | null =
    !freshnessResult && !schedulersResult
      ? null
      : freshnessResult?.ok || schedulersResult?.ok
        ? {
            ok: true,
            data: {
              cards: buildIngestionCards(freshnessResult, schedulersResult),
            },
          }
        : {
            ok: false,
            error:
              freshnessResult?.error ??
              schedulersResult?.error ??
              "暫時無法顯示導入概況",
          }
  const states: IngestionCardState[] = (
    result?.ok ? result.data.cards : []
  ).map(card => ({ card, runtime: describeSchedulerRuntime(card) }))
  const schedulers = schedulersResult?.ok ? schedulersResult.data.data : []
  const actions = useSchedulerActions(audit, applyScheduler)
  return (
    <Panel
      eyebrow="Ingestion overview"
      title="導入與排程"
      icon={<CalendarClock size={19} />}
      result={result}
      loading={loading}
    >
      {result?.ok && (
        <div className="grid gap-6">
          <p className="m-0 text-xs text-muted">
            部署前人工確認：請停止所有排程，確認每張卡片的「Owner
            設定」與「Runtime 實況」均為已停止。
          </p>
          <section
            aria-label="資料抓取排程"
            className="grid gap-4 rounded-2xl border border-line bg-surface-soft p-4 sm:p-5"
          >
            <p className="m-0 text-sm text-muted">
              依核准的全市場範圍每日抓取資料；啟動前須完成治理與啟用資格。
              「Owner 設定」是 Owner 的意圖，「Runtime 實況」是 Fetcher runtime
              回報的實際狀態。
            </p>
            <SchedulerBulkControls
              role={role}
              schedulers={schedulers}
              states={states}
              actions={actions}
            />
            {states.length === 0 ? (
              <EmptyState>目前沒有已註冊的資料抓取排程。</EmptyState>
            ) : (
              <div className="grid gap-3">
                {[...new Set(states.flatMap(({ card }) => cardMarkets(card)))]
                  .sort()
                  .map(market => (
                    <section key={market} className="grid gap-3">
                      <h3 className="font-bold">{market}</h3>
                      {states
                        .filter(({ card }) =>
                          cardMarkets(card).includes(market)
                        )
                        .map(state => (
                          <IngestionCardView
                            state={state}
                            key={state.card.control.scheduler_key}
                            role={role}
                            pending={
                              actions.bulkPending ||
                              actions.pendingKeys.size > 0
                            }
                            actionError={
                              actions.actionErrors[
                                state.card.control.scheduler_key
                              ]
                            }
                            requestToggle={actions.requestToggleScheduler}
                          />
                        ))}
                    </section>
                  ))}
              </div>
            )}
          </section>
        </div>
      )}
      {actions.dialog}
    </Panel>
  )
}

/** Compatibility panel for integrations that still render freshness alone. */
export function MarketFreshnessPanel({
  result,
  loading,
  refreshError,
}: {
  result: PanelResult<MarketFreshnessResponse> | null
  loading: boolean
  refreshError: string
}) {
  const summaries = result?.ok ? result.data.data : []
  const grouped = [...new Set(summaries.map(summary => summary.market))].sort()
  return (
    <Panel
      eyebrow="Market freshness"
      title="市場資料更新"
      icon={<CalendarClock size={19} />}
      result={result}
      loading={loading}
    >
      {refreshError && result?.ok && (
        <Alert className="mb-4" variant="warning" role="status">
          <AlertTriangle size={18} />
          <AlertDescription>
            目前保留上次成功資料：{refreshError}
          </AlertDescription>
        </Alert>
      )}
      {summaries.length === 0 ? (
        <EmptyState>目前沒有已啟用的市場排程。</EmptyState>
      ) : (
        <div className="grid gap-5">
          {grouped.map(market => (
            <section key={market}>
              <h3 className="mb-2 font-bold">{market}</h3>
              <div className="grid gap-2 xl:grid-cols-2">
                {summaries
                  .filter(summary => summary.market === market)
                  .map(summary => (
                    <Card key={summary.scheduler_key}>
                      <div className="flex items-start justify-between gap-2">
                        <strong className="text-lg">{summary.market}</strong>
                        <Badge variant={freshnessVariant(summary.status)}>
                          {STATUS_LABELS[summary.status]}
                        </Badge>
                      </div>
                      <p className="mt-1 text-xs text-muted">
                        下次{" "}
                        <DateWithRelative value={summary.next_scheduled_at} />
                      </p>
                      <details className="mt-3 rounded-lg bg-surface-soft px-3 py-2">
                        <summary className="cursor-pointer text-xs font-bold text-accent">
                          Feed 明細（{summary.feeds.length}）
                        </summary>
                        <FeedDetails feeds={summary.feeds} />
                      </details>
                    </Card>
                  ))}
              </div>
            </section>
          ))}
        </div>
      )}
    </Panel>
  )
}

export function OperationsOverviewPage({ role }: { role?: AdminRole }) {
  const effectiveRole = role ?? "viewer"
  const audit: DashboardRequest["audit"] = { ...OPERATIONS_OVERVIEW_AUDIT }
  const query = useOperationsDashboardQuery("overview", audit)
  const state = useOperationsDashboardState(query)
  const overview = state.response?.view === "overview" ? state.response : null
  const freshnessError = state.errors.freshness ?? ""
  const schedulersError = state.errors.schedulers ?? ""
  return (
    <>
      <PageIntro
        eyebrow="Ingestion health"
        title="導入概況"
        description="檢查市場資料更新、佇列、Worker heartbeat 與 outbox 的即時健康狀態。"
      />
      <OperationsDashboardStatus state={state} />
      <div className="grid gap-5">
        <IngestionOverviewPanel
          freshnessResult={overview?.freshness ?? null}
          schedulersResult={overview?.schedulers ?? null}
          loading={state.initialLoading}
          pending={state.pending}
          freshnessError={freshnessError}
          schedulersError={schedulersError}
          role={effectiveRole}
          audit={audit}
        />
        <Panel
          eyebrow="Queue and worker"
          title="Source ingest 後的佇列與 Worker"
          icon={<Database size={19} />}
          result={overview?.queue ?? null}
          loading={state.initialLoading}
        >
          {overview?.queue.ok && (
            <>
              <p className="mb-3 text-xs text-muted">
                Source ingest 已寫入 durable queue 後，這裡顯示
                dispatcher、outbox 與 normalization worker 的處理狀態。
              </p>
              <div className="grid grid-cols-2 gap-2 sm:grid-cols-4">
                <Metric
                  label="排隊中"
                  value={overview.queue.data.counts.queued ?? 0}
                />
                <Metric
                  label="處理中"
                  value={overview.queue.data.counts.processing ?? 0}
                />
                <Metric
                  label="重試耗盡"
                  value={overview.queue.data.retry_exhausted}
                />
                <Metric
                  label="過期租約"
                  value={overview.queue.data.expired_leases}
                />
                <Metric
                  wide
                  label="Worker heartbeat"
                  value={formatAge(
                    overview.queue.data.worker_heartbeat_age_seconds
                  )}
                  detail={formatDate(
                    overview.queue.data.last_worker_heartbeat_at
                  )}
                />
                <Metric
                  wide
                  label="未發布 outbox"
                  value={overview.queue.data.unpublished_outbox}
                />
              </div>
            </>
          )}
        </Panel>
      </div>
      <Alert className="mt-5" variant="subtle" role="note">
        <AlertTitle>目前限制</AlertTitle>
        <AlertDescription>
          後端尚無歷史 run 趨勢 API，因此本頁只呈現即時佇列狀態。
        </AlertDescription>
      </Alert>
    </>
  )
}

// Re-exported for tests and consumers that use the shared query helpers.
export type { OperationsDashboardQuery }
