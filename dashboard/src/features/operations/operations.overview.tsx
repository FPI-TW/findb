import { useQueryClient } from "@tanstack/react-query"
import { useServerFn } from "@tanstack/react-start"
import {
  AlertTriangle,
  CalendarClock,
  Database,
  Power,
  TriangleAlert,
} from "lucide-react"
import { useState, type ReactNode } from "react"

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
  formatSchedulerState,
  schedulerStateVariant,
  PageIntro,
  Panel,
  RefreshStatus,
  DateWithRelative,
  EmptyState,
  Metric,
  freshnessVariant,
  SLOT_SEMANTIC_LABELS,
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
  | "full_market_enabled"
  | "environment"
  | "admitted_dataset_keys"
  | "first_start_dates"
  | "feed_readiness"
>

type IngestionCard = {
  control: SchedulerCardControl
  freshness: MarketFreshness | null
}

function controlFromFreshness(summary: MarketFreshness): SchedulerCardControl {
  return {
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

export type SchedulerRuntimeStatus =
  | "configuration_error"
  | "not_activated"
  | "deactivated"
  | "not_reported"
  | "stale"
  | "stopping"
  | "stopped"
  | "running"
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
  stopped: { label: "停止", variant: "secondary" },
  running: { label: "執行中", variant: "default" },
  unknown: { label: "未知錯誤", variant: "destructive" },
} satisfies Record<SchedulerRuntimeStatus, SchedulerRuntimeStatusMeta>

export function selectSchedulerRuntimeStatus({
  control,
  freshness,
}: {
  control: Pick<
    SchedulerCardControl,
    | "desired_state"
    | "observed_state"
    | "heartbeat_age_seconds"
    | "last_heartbeat_at"
  >
  freshness:
    | (Pick<MarketFreshness, "configuration_status" | "configuration_errors"> &
        Partial<Pick<MarketFreshness, "monitor_kind" | "activation_state">>)
    | null
}): SchedulerRuntimeStatus {
  if (
    freshness?.configuration_status === "error" ||
    (freshness?.configuration_errors.length ?? 0) > 0 ||
    (freshness?.monitor_kind === "full_market" &&
      control.desired_state === "running" &&
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
  if (control.heartbeat_age_seconds > 90) return "stale"
  if (control.desired_state === "stopped") {
    if (control.observed_state === "running") return "stopping"
    if (control.observed_state === "stopped") return "stopped"
    return "unknown"
  }
  if (control.desired_state === "running") return "running"
  return "unknown"
}

const SCHEDULER_CYCLE_LABELS: Record<Scheduler["observed_state"], string> = {
  running: "Cycle：執行中",
  stopped: "Cycle：閒置",
}

function schedulerCycleLabel(value: Scheduler["observed_state"]) {
  return SCHEDULER_CYCLE_LABELS[value] ?? "Cycle：未知"
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

const SCHEDULER_PROFILES = ["pilot", "full_market"] as const
export type SchedulerProfile = (typeof SCHEDULER_PROFILES)[number]

function schedulerProfile(control: Pick<Scheduler, "scheduler_key">) {
  return control.scheduler_key.startsWith("full_market_")
    ? "full_market"
    : "pilot"
}

function schedulerProfileLabel(profile: SchedulerProfile) {
  return profile === "full_market" ? "Full market" : "Pilot"
}

function overlappingSchedulers(
  targets: SchedulerMutationTarget[],
  schedulers: SchedulerMutationTarget[]
) {
  return schedulers.filter(
    scheduler =>
      (scheduler.desired_state === "running" ||
        scheduler.observed_state === "running") &&
      targets.some(
        target =>
          target.provider === scheduler.provider &&
          schedulerProfile(target) !== schedulerProfile(scheduler)
      )
  )
}

function SchedulerOverlapWarning({
  schedulers,
}: {
  schedulers: SchedulerMutationTarget[]
}) {
  if (schedulers.length === 0) return null
  return (
    <Alert variant="warning">
      <AlertTriangle aria-hidden="true" />
      <AlertTitle>Full market 與 Pilot 將同時執行</AlertTitle>
      <AlertDescription>
        <p className="m-0">
          同一 provider 的兩種排程範圍重疊，可能重複抓取、增加 API
          用量與資料導入工作。
          建議先停止另一區排程，並等待實際狀態變為已停止，再啟動。
        </p>
        <ul className="my-0 max-h-40 overflow-auto pl-5 font-mono text-xs wrap-anywhere">
          {schedulers.map(scheduler => (
            <li key={scheduler.scheduler_key}>
              {scheduler.provider} / {scheduler.scheduler_key}（期望：
              {formatSchedulerState(scheduler.desired_state)}；實際：
              {formatSchedulerState(scheduler.observed_state)}）
            </li>
          ))}
        </ul>
      </AlertDescription>
    </Alert>
  )
}

function schedulerStartAllowed(control: SchedulerMutationTarget) {
  return (
    control.start_allowed ?? !control.scheduler_key.startsWith("full_market_")
  )
}

function schedulerStartReason(control: SchedulerMutationTarget) {
  const reasons: Record<string, string> = {
    full_market_no_enabled_datasets: "尚未啟用任何全市場 feed",
    full_market_flag_disabled: "環境 Full market flag 關閉",
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
    full_market_calendar_missing: "缺少完整已發布的交易所日曆",
  }
  return control.start_blockers?.length
    ? control.start_blockers
        .map(code => reasons[code] ?? "全市場設定或 scope 無效")
        .join("；")
    : "缺少全市場啟動資格，請重新整理並確認治理設定"
}

type SchedulerBulkConfirmation = {
  profile: SchedulerProfile
  desiredState: SchedulerDesiredState
  targets: SchedulerMutationTarget[]
  skipped: SchedulerMutationTarget[]
}

function SchedulerConfirmationDialog({
  target,
  overlaps,
  onCancel,
  onConfirm,
}: {
  target: SchedulerMutationTarget | null
  overlaps: SchedulerMutationTarget[]
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
          <SchedulerOverlapWarning schedulers={overlaps} />
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
  overlaps,
  onCancel,
  onConfirm,
}: {
  confirmation: SchedulerBulkConfirmation | null
  overlaps: SchedulerMutationTarget[]
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
            <AlertDialogTitle>
              確認 {schedulerProfileLabel(confirmation.profile)} {actionLabel}？
            </AlertDialogTitle>
            <AlertDialogDescription>
              將依序更新 {confirmation.targets.length} 個
              Scheduler，且每筆都會以目前 revision 防止覆蓋其他人剛完成的操作。
              {confirmation.desiredState === "stopped"
                ? "送出後仍須等待每張卡片的期望與實際狀態都顯示為已停止，才能開始部署。"
                : "送出後請逐一確認實際狀態恢復執行，部分失敗不會自動重試。"}
            </AlertDialogDescription>
          </AlertDialogHeader>
          <SchedulerOverlapWarning schedulers={overlaps} />
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
              確認 {schedulerProfileLabel(confirmation.profile)} {actionLabel}
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      )}
    </AlertDialog>
  )
}

function useSchedulerActions(
  schedulers: SchedulerMutationTarget[],
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
          `${schedulerProfileLabel(schedulerProfile(targets[0]!))} Scheduler 已全部${desiredState === "running" ? "啟動" : "停止"}`,
          { description: `成功更新 ${succeeded} 筆；請繼續確認實際狀態。` }
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
      profile: SchedulerProfile,
      desiredState: SchedulerDesiredState,
      controls: SchedulerMutationTarget[]
    ) => {
      if (bulkPending || pendingKeys.size > 0) return
      const candidates = controls.filter(
        scheduler =>
          schedulerProfile(scheduler) === profile &&
          scheduler.desired_state !== desiredState
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
        setBulkConfirmation({ profile, desiredState, targets, skipped })
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
          overlaps={
            confirmationTarget?.desired_state === "stopped"
              ? overlappingSchedulers([confirmationTarget], schedulers)
              : []
          }
          onCancel={() => setConfirmationTarget(null)}
          onConfirm={() => {
            const target = confirmationTarget
            setConfirmationTarget(null)
            if (target) void toggleScheduler(target)
          }}
        />
        <SchedulerBulkConfirmationDialog
          confirmation={bulkConfirmation}
          overlaps={
            bulkConfirmation?.desiredState === "running"
              ? overlappingSchedulers(bulkConfirmation.targets, schedulers)
              : []
          }
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

function SchedulerBulkControls({
  profile,
  role,
  schedulers,
  actions,
}: {
  profile: SchedulerProfile
  role: AdminRole
  schedulers: SchedulerMutationTarget[]
  actions: ReturnType<typeof useSchedulerActions>
}) {
  if (schedulers.length === 0) return null
  const blocked = schedulers.filter(
    control =>
      control.desired_state !== "running" && !schedulerStartAllowed(control)
  )

  return (
    <div className="mb-4 grid gap-3 rounded-xl border border-line bg-surface p-4 sm:grid-cols-[1fr_auto] sm:items-center">
      <div>
        <p className="m-0 text-sm font-semibold">
          {schedulerProfileLabel(profile)} 批次控制
        </p>
        <p className="mt-1 mb-0 text-xs text-muted">
          只更新此區排程。停止後請等待此區所有卡片的「期望」與「實際」均為已停止。
        </p>
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
      {role === "owner" && (
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
            onClick={() =>
              actions.requestBulkState(profile, "stopped", schedulers)
            }
          >
            <Power aria-hidden="true" />
            {schedulerProfileLabel(profile)} 全部停止
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
            onClick={() =>
              actions.requestBulkState(profile, "running", schedulers)
            }
          >
            <Power aria-hidden="true" />
            {schedulerProfileLabel(profile)} 全部啟動
          </Button>
        </div>
      )}
    </div>
  )
}

function SchedulerProfileSection({
  profile,
  role,
  schedulers,
  actions,
  children,
}: {
  profile: SchedulerProfile
  role: AdminRole
  schedulers: SchedulerMutationTarget[]
  actions: ReturnType<typeof useSchedulerActions>
  children: ReactNode
}) {
  return (
    <section
      aria-label={`${schedulerProfileLabel(profile)} 排程`}
      className="grid gap-4 rounded-2xl border border-line bg-surface-soft p-4 sm:p-5"
    >
      <div>
        <h3 className="m-0 text-base font-bold">
          {schedulerProfileLabel(profile)}
          {profile === "full_market" ? "（全市場）" : "（小範圍驗證）"}
        </h3>
        <p className="mt-1 mb-0 text-sm text-muted">
          {profile === "full_market"
            ? "依核准的全市場範圍抓取資料；啟動前須完成治理與啟用資格。"
            : "使用少量商品驗證資料流程；同 provider 的商品可能包含在全市場範圍內。"}
        </p>
      </div>
      <SchedulerBulkControls
        profile={profile}
        role={role}
        schedulers={schedulers}
        actions={actions}
      />
      {children}
    </section>
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
        aria-label={`${schedulerProfileLabel(schedulerProfile(control))} ${control.provider} ${control.scheduler_key} 設為${nextState === "running" ? "執行中" : "已停止"}`}
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

function IngestionCardView({
  card,
  role,
  pending,
  actionError,
  requestToggle,
}: {
  card: IngestionCard
  role: AdminRole
  pending: boolean
  actionError: string | undefined
  requestToggle: (control: SchedulerMutationTarget) => void
}) {
  const { control, freshness } = card
  const runtimeStatus = selectSchedulerRuntimeStatus({ control, freshness })
  const runtimeStatusMeta = SCHEDULER_RUNTIME_STATUS_META[runtimeStatus]
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
        <p className="mt-1 mb-0 text-xs text-muted">
          每日排程 {formatScheduledTime(control.scheduled_local_time)} ·{" "}
          {control.timezone} · Slot {control.slot_id}
        </p>
      </div>
      <dl className="grid gap-3 text-sm sm:grid-cols-2 lg:grid-cols-4">
        <div>
          <dt className="text-xs text-muted">Scheduler 狀態</dt>
          <dd className="mt-0.5 flex flex-wrap items-center gap-2">
            <Badge variant={runtimeStatusMeta.variant}>
              {runtimeStatusMeta.label}
            </Badge>
            <span className="text-xs text-muted">
              {schedulerCycleLabel(control.observed_state)}
            </span>
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
        <div>
          <dt className="text-xs text-muted">Heartbeat</dt>
          <dd className="mt-0.5">
            {formatAge(control.heartbeat_age_seconds)}
            <span className="mx-1 text-muted">·</span>
            <DateWithRelative value={control.last_heartbeat_at} />
          </dd>
        </div>
        <div>
          <dt className="text-xs text-muted">Revision</dt>
          <dd className="mt-0.5 font-mono">r{control.revision}</dd>
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
      {schedulerProfile(control) === "full_market" && (
        <div className="grid gap-2 text-xs text-muted">
          <p className="m-0">
            FULL_MARKET_ENABLED：
            {control.full_market_enabled ? "true" : "false"} ·{" "}
            {control.environment ?? "—"}
          </p>
          <p className="m-0">
            本次凍結範圍：{control.admitted_dataset_keys?.join(", ") || "無"}
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
            全市場已啟用範圍：{freshness.active_dataset_keys.join(", ") || "無"}
            。 Feed 明細可能來自 bounded runtime；資料更新不代表全市場已啟用或此
            Scheduler 已回報心跳。
          </p>
          {!control.last_heartbeat_at && (
            <p className="m-0">
              此全市場 runtime 尚未回報；請確認部署與 Owner 啟用狀態。
            </p>
          )}
          {freshness.pending_feeds.length > 0 && (
            <div>
              <strong>待啟用 Feed／readiness 前提：</strong>
              <ul className="mt-1 mb-0 list-disc pl-5">
                {freshness.pending_feeds.map(feed => (
                  <li key={feed.dataset_key}>
                    {feed.dataset_key}：{feed.blockers.join("；")}
                  </li>
                ))}
              </ul>
            </div>
          )}
        </div>
      )}
      {control.last_error && (
        <Alert variant="warning" role="status">
          <TriangleAlert size={17} />
          <AlertDescription>
            <span className="font-semibold">最近錯誤：</span>
            {control.last_error}
          </AlertDescription>
        </Alert>
      )}
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
  profile: selectedProfile = "pilot",
  updateProfile = () => undefined,
  freshnessResult,
  schedulersResult,
  loading,
  role,
  applyScheduler,
  audit = OPERATIONS_OVERVIEW_AUDIT,
}: {
  profile?: SchedulerProfile
  updateProfile?: (profile: SchedulerProfile) => void
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
  const cards = result?.ok ? result.data.cards : []
  const schedulers = schedulersResult?.ok ? schedulersResult.data.data : []
  const actions = useSchedulerActions(
    cards.map(card => card.control),
    audit,
    applyScheduler
  )
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
            部署前人工確認：請分別停止 Full market 與
            Pilot，確認所有排程的期望與實際狀態均為已停止。
          </p>
          <div role="tablist" aria-label="導入排程模式" className="flex gap-2">
            {SCHEDULER_PROFILES.map(profile => (
              <Button
                key={profile}
                role="tab"
                id={`scheduler-tab-${profile}`}
                aria-controls={`scheduler-panel-${profile}`}
                aria-selected={selectedProfile === profile}
                tabIndex={selectedProfile === profile ? 0 : -1}
                variant={selectedProfile === profile ? "default" : "outline"}
                onClick={() => updateProfile(profile)}
                onKeyDown={event => {
                  const index = SCHEDULER_PROFILES.indexOf(profile)
                  const nextIndex =
                    event.key === "Home"
                      ? 0
                      : event.key === "End"
                        ? SCHEDULER_PROFILES.length - 1
                        : event.key === "ArrowRight"
                          ? (index + 1) % SCHEDULER_PROFILES.length
                          : event.key === "ArrowLeft"
                            ? (index + SCHEDULER_PROFILES.length - 1) %
                              SCHEDULER_PROFILES.length
                            : null
                  if (nextIndex === null) return
                  event.preventDefault()
                  const next = SCHEDULER_PROFILES[nextIndex]!
                  event.currentTarget.parentElement
                    ?.querySelectorAll<HTMLButtonElement>('[role="tab"]')
                    [nextIndex]?.focus()
                  updateProfile(next)
                }}
              >
                {schedulerProfileLabel(profile)}（
                {
                  cards.filter(
                    card =>
                      schedulerProfile(card.control) === profile &&
                      card.control.desired_state === "running"
                  ).length
                }{" "}
                執行中）
              </Button>
            ))}
          </div>
          {SCHEDULER_PROFILES.filter(
            profile => profile === selectedProfile
          ).map(profile => {
            const profileCards = cards.filter(
              card => schedulerProfile(card.control) === profile
            )
            return (
              <div
                key={profile}
                role="tabpanel"
                id={`scheduler-panel-${profile}`}
                aria-labelledby={`scheduler-tab-${profile}`}
              >
                <SchedulerProfileSection
                  profile={profile}
                  role={role}
                  schedulers={schedulers.filter(
                    scheduler => schedulerProfile(scheduler) === profile
                  )}
                  actions={actions}
                >
                  {profileCards.length === 0 ? (
                    <EmptyState>此區目前沒有已註冊的資料抓取排程。</EmptyState>
                  ) : (
                    <div className="grid gap-3">
                      {profileCards.map(card => (
                        <IngestionCardView
                          card={card}
                          key={card.control.scheduler_key}
                          role={role}
                          pending={
                            actions.bulkPending || actions.pendingKeys.size > 0
                          }
                          actionError={
                            actions.actionErrors[card.control.scheduler_key]
                          }
                          requestToggle={actions.requestToggleScheduler}
                        />
                      ))}
                    </div>
                  )}
                </SchedulerProfileSection>
              </div>
            )
          })}
        </div>
      )}
      {actions.dialog}
    </Panel>
  )
}

export function SchedulerPanel({
  result,
  loading,
  pending,
  refreshError,
  role,
  applyScheduler,
}: {
  result: PanelResult<SchedulersResponse> | null
  loading: boolean
  pending: boolean
  refreshError: string
  role: AdminRole
  applyScheduler?: (response: SchedulerMutationResponse) => void
}) {
  const schedulers = result?.ok ? result.data.data : []
  const actions = useSchedulerActions(
    schedulers,
    OPERATIONS_OVERVIEW_AUDIT,
    applyScheduler
  )
  return (
    <Panel
      eyebrow="Scheduler control"
      title="資料抓取排程"
      icon={<Power size={19} />}
      result={result}
      loading={loading}
    >
      {!loading && (
        <RefreshStatus
          pending={pending}
          label="正在更新排程狀態…目前資料仍可操作。"
        />
      )}
      {refreshError && result?.ok && (
        <Alert className="mb-4" variant="warning" role="status">
          <AlertTriangle size={18} />
          <AlertTitle>排程狀態暫時無法重新取得</AlertTitle>
          <AlertDescription>
            目前保留上次成功資料：{refreshError}
          </AlertDescription>
        </Alert>
      )}
      {result?.ok && (
        <div className="grid gap-6">
          <p className="m-0 text-xs text-muted">
            部署前人工確認：請分別停止 Full market 與
            Pilot，確認所有排程的期望與實際狀態均為已停止。
          </p>
          {SCHEDULER_PROFILES.map(profile => {
            const profileSchedulers = schedulers.filter(
              scheduler => schedulerProfile(scheduler) === profile
            )
            return (
              <SchedulerProfileSection
                key={profile}
                profile={profile}
                role={role}
                schedulers={profileSchedulers}
                actions={actions}
              >
                {profileSchedulers.length === 0 ? (
                  <EmptyState>此區目前沒有已註冊的資料抓取排程。</EmptyState>
                ) : (
                  <div className="grid gap-3">
                    {profileSchedulers.map(scheduler => {
                      const stale =
                        scheduler.heartbeat_age_seconds === null ||
                        scheduler.heartbeat_age_seconds > 90
                      const actionError =
                        actions.actionErrors[scheduler.scheduler_key]
                      return (
                        <article
                          className="grid gap-4 rounded-xl border border-line bg-surface p-4"
                          key={scheduler.scheduler_key}
                        >
                          <div className="flex flex-wrap items-start justify-between gap-3">
                            <div className="min-w-0">
                              <h3 className="m-0 font-mono text-sm font-bold wrap-anywhere">
                                {scheduler.scheduler_key}
                              </h3>
                              <p className="mt-1 mb-0 text-xs text-muted">
                                Provider：{scheduler.provider} · Dataset：
                                {scheduler.dataset_keys.join(", ") || "—"}
                              </p>
                            </div>
                            <div className="flex flex-wrap items-center justify-end gap-1.5">
                              <Badge
                                variant={schedulerStateVariant(
                                  scheduler.desired_state
                                )}
                              >
                                期望：
                                {formatSchedulerState(scheduler.desired_state)}
                              </Badge>
                              <Badge
                                variant={schedulerStateVariant(
                                  scheduler.observed_state
                                )}
                              >
                                實際：
                                {formatSchedulerState(scheduler.observed_state)}
                              </Badge>
                              {stale && (
                                <Badge variant="destructive">
                                  <TriangleAlert aria-hidden="true" />
                                  Heartbeat stale
                                </Badge>
                              )}
                            </div>
                          </div>
                          <dl className="grid gap-3 text-sm sm:grid-cols-2 lg:grid-cols-4">
                            <div>
                              <dt className="text-xs text-muted">Heartbeat</dt>
                              <dd className="mt-0.5">
                                {formatAge(scheduler.heartbeat_age_seconds)} ·{" "}
                                <DateWithRelative
                                  value={scheduler.last_heartbeat_at}
                                />
                              </dd>
                            </div>
                            <div>
                              <dt className="text-xs text-muted">
                                最近 cycle 開始
                              </dt>
                              <dd className="mt-0.5">
                                <DateWithRelative
                                  value={scheduler.last_cycle_started_at}
                                />
                              </dd>
                            </div>
                            <div>
                              <dt className="text-xs text-muted">
                                最近 cycle 完成
                              </dt>
                              <dd className="mt-0.5">
                                <DateWithRelative
                                  value={scheduler.last_cycle_completed_at}
                                />
                              </dd>
                            </div>
                            <div>
                              <dt className="text-xs text-muted">Revision</dt>
                              <dd className="mt-0.5 font-mono">
                                r{scheduler.revision}
                              </dd>
                            </div>
                          </dl>
                          {scheduler.last_error && (
                            <Alert variant="warning" role="status">
                              <TriangleAlert size={17} />
                              <AlertDescription>
                                <span className="font-semibold">
                                  最近錯誤：
                                </span>
                                {scheduler.last_error}
                              </AlertDescription>
                            </Alert>
                          )}
                          <div className="flex flex-wrap items-center justify-between gap-2 border-t border-line pt-3">
                            <SchedulerActionButton
                              role={role}
                              control={scheduler}
                              pending={
                                actions.bulkPending ||
                                actions.pendingKeys.size > 0
                              }
                              requestToggle={actions.requestToggleScheduler}
                            />
                            {actionError && (
                              <p
                                className="m-0 text-xs text-danger"
                                role="alert"
                              >
                                {actionError}
                              </p>
                            )}
                          </div>
                        </article>
                      )
                    })}
                  </div>
                )}
              </SchedulerProfileSection>
            )
          })}
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
  const grouped = [...new Set(summaries.map(summary => summary.slot_id))].sort()
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
          {grouped.map(slotId => (
            <section key={slotId}>
              <h3 className="mb-2 font-bold">
                {SLOT_SEMANTIC_LABELS[slotId] ?? slotId}
              </h3>
              <div className="grid gap-2 xl:grid-cols-2">
                {summaries
                  .filter(summary => summary.slot_id === slotId)
                  .map(summary => (
                    <Card key={`${summary.slot_id}:${summary.market}`}>
                      <div className="flex items-start justify-between gap-2">
                        <strong className="text-lg">{summary.market}</strong>
                        <Badge variant={freshnessVariant(summary.status)}>
                          {STATUS_LABELS[summary.status]}
                        </Badge>
                      </div>
                      <p className="mt-1 text-xs text-muted">
                        排程 {formatScheduledTime(summary.scheduled_local_time)}{" "}
                        · 下次{" "}
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

export function OperationsOverviewPage({
  role,
  profile = "pilot",
  updateProfile = () => undefined,
}: {
  role?: AdminRole
  profile?: SchedulerProfile
  updateProfile?: (profile: SchedulerProfile) => void
}) {
  const effectiveRole = role ?? "viewer"
  const audit: DashboardRequest["audit"] = {
    ...OPERATIONS_OVERVIEW_AUDIT,
  }
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
          profile={profile}
          updateProfile={updateProfile ?? (() => undefined)}
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
