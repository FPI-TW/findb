import { useQueryClient } from "@tanstack/react-query"
import { useNavigate } from "@tanstack/react-router"
import { useServerFn } from "@tanstack/react-start"
import {
  AlertTriangle,
  CalendarClock,
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
import { isDashboardAuthenticationError } from "../../lib/auth-errors"
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
  operationsKeys,
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
  freshness: Pick<
    MarketFreshness,
    "configuration_status" | "configuration_errors"
  > | null
}): SchedulerRuntimeStatus {
  if (
    freshness?.configuration_status === "error" ||
    (freshness?.configuration_errors.length ?? 0) > 0
  ) {
    return "configuration_error"
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
  "scheduler_key" | "provider" | "desired_state" | "revision"
>

type SchedulerBulkConfirmation = {
  desiredState: SchedulerDesiredState
  targets: SchedulerMutationTarget[]
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
        <AlertDialogContent>
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
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>確認{actionLabel} Scheduler？</AlertDialogTitle>
            <AlertDialogDescription>
              將依序更新 {confirmation.targets.length} 個
              Scheduler，且每筆都會以目前 revision 防止覆蓋其他人剛完成的操作。
              {confirmation.desiredState === "stopped"
                ? "送出後仍須等待每張卡片的期望與實際狀態都顯示為已停止，才能開始部署。"
                : "送出後請逐一確認實際狀態恢復執行，部分失敗不會自動重試。"}
            </AlertDialogDescription>
          </AlertDialogHeader>
          <ul className="my-0 max-h-48 overflow-auto pl-5 font-mono text-xs text-muted">
            {confirmation.targets.map(target => (
              <li key={target.scheduler_key}>
                {target.provider} / {target.scheduler_key} / r{target.revision}
              </li>
            ))}
          </ul>
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
  const navigate = useNavigate()
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
    if (pendingKeys.has(control.scheduler_key)) return
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
      if (isDashboardAuthenticationError(reason)) {
        await navigate({ to: "/login", replace: true })
        queryClient.removeQueries({ queryKey: operationsKeys.root })
        return
      }
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
          if (isDashboardAuthenticationError(reason)) {
            authenticationFailed = true
            await navigate({ to: "/login", replace: true })
            queryClient.removeQueries({ queryKey: operationsKeys.root })
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
          `已${desiredState === "running" ? "啟動" : "停止"}全部 Scheduler`,
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
    confirmationTarget,
    setConfirmationTarget,
    requestBulkState: (
      schedulers: SchedulerMutationTarget[],
      desiredState: SchedulerDesiredState
    ) => {
      if (bulkPending || pendingKeys.size > 0) return
      const targets = schedulers.filter(
        scheduler => scheduler.desired_state !== desiredState
      )
      if (targets.length > 0) setBulkConfirmation({ desiredState, targets })
    },
    requestToggleScheduler: (control: SchedulerMutationTarget) => {
      if (!bulkPending && !pendingKeys.has(control.scheduler_key))
        setConfirmationTarget(control)
    },
    confirm: () => {
      const target = confirmationTarget
      setConfirmationTarget(null)
      if (target) void toggleScheduler(target)
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

function SchedulerBulkControls({
  role,
  schedulers,
  actions,
}: {
  role: AdminRole
  schedulers: SchedulerMutationTarget[]
  actions: ReturnType<typeof useSchedulerActions>
}) {
  if (schedulers.length === 0) return null

  return (
    <div className="mb-4 grid gap-3 rounded-xl border border-line bg-surface p-4 sm:grid-cols-[1fr_auto] sm:items-center">
      <div>
        <p className="m-0 text-sm font-semibold">部署前人工確認</p>
        <p className="mt-1 mb-0 text-xs text-muted">
          Fetcher
          部署前請先全部停止，並等待所有卡片的「期望」與「實際」均為已停止；部署仍會
          fail closed，不會自動變更 Scheduler 狀態。
        </p>
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
            onClick={() => actions.requestBulkState(schedulers, "stopped")}
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
                scheduler => scheduler.desired_state === "running"
              )
            }
            aria-busy={actions.bulkPending}
            onClick={() => actions.requestBulkState(schedulers, "running")}
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
    <Button
      type="button"
      variant={control.desired_state === "running" ? "destructive" : "default"}
      onClick={() => requestToggle(control)}
      disabled={pending}
      aria-busy={pending}
      aria-label={`${control.provider} 設為${nextState === "running" ? "執行中" : "已停止"}`}
    >
      <Power aria-hidden="true" />
      {pending ? "更新中…" : nextState === "running" ? "啟用排程" : "停止排程"}
    </Button>
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
  freshnessResult,
  schedulersResult,
  loading,
  pending,
  freshnessError,
  schedulersError,
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
  const actions = useSchedulerActions(audit, applyScheduler)
  const cards = result?.ok ? result.data.cards : []
  const schedulers = schedulersResult?.ok ? schedulersResult.data.data : []
  return (
    <Panel
      eyebrow="Ingestion overview"
      title="導入與排程"
      icon={<CalendarClock size={19} />}
      result={result}
      loading={loading}
    >
      {(freshnessError || schedulersError) && result?.ok && (
        <Alert className="mb-4" variant="warning" role="status">
          <AlertTriangle size={18} />
          <AlertTitle>部分導入狀態暫時無法重新取得</AlertTitle>
          <AlertDescription>
            目前保留上次成功資料：
            {[freshnessError, schedulersError].filter(Boolean).join("；")}
          </AlertDescription>
        </Alert>
      )}
      {!loading && (
        <RefreshStatus
          pending={pending}
          label="正在更新導入狀態…目前資料仍可操作。"
        />
      )}
      {result?.ok && (
        <SchedulerBulkControls
          role={role}
          schedulers={schedulers}
          actions={actions}
        />
      )}
      {result?.ok &&
        (cards.length === 0 ? (
          <Alert variant="warning" role="status">
            <AlertTriangle size={18} />
            <AlertDescription>目前沒有已註冊的資料抓取排程。</AlertDescription>
          </Alert>
        ) : (
          <div className="grid gap-3">
            {cards.map(card => (
              <IngestionCardView
                card={card}
                key={card.control.scheduler_key}
                role={role}
                pending={
                  actions.bulkPending ||
                  actions.pendingKeys.has(card.control.scheduler_key)
                }
                actionError={actions.actionErrors[card.control.scheduler_key]}
                requestToggle={actions.requestToggleScheduler}
              />
            ))}
          </div>
        ))}
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
  const actions = useSchedulerActions(OPERATIONS_OVERVIEW_AUDIT, applyScheduler)
  const schedulers = result?.ok ? result.data.data : []
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
        <SchedulerBulkControls
          role={role}
          schedulers={schedulers}
          actions={actions}
        />
      )}
      {result?.ok &&
        (schedulers.length === 0 ? (
          <EmptyState>目前沒有已註冊的資料抓取排程。</EmptyState>
        ) : (
          <div className="grid gap-3">
            {schedulers.map(scheduler => {
              const stale =
                scheduler.heartbeat_age_seconds === null ||
                scheduler.heartbeat_age_seconds > 90
              const actionError = actions.actionErrors[scheduler.scheduler_key]
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
                        variant={schedulerStateVariant(scheduler.desired_state)}
                      >
                        期望：{formatSchedulerState(scheduler.desired_state)}
                      </Badge>
                      <Badge
                        variant={schedulerStateVariant(
                          scheduler.observed_state
                        )}
                      >
                        實際：{formatSchedulerState(scheduler.observed_state)}
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
                        <DateWithRelative value={scheduler.last_heartbeat_at} />
                      </dd>
                    </div>
                    <div>
                      <dt className="text-xs text-muted">最近 cycle 開始</dt>
                      <dd className="mt-0.5">
                        <DateWithRelative
                          value={scheduler.last_cycle_started_at}
                        />
                      </dd>
                    </div>
                    <div>
                      <dt className="text-xs text-muted">最近 cycle 完成</dt>
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
                        <span className="font-semibold">最近錯誤：</span>
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
                        actions.pendingKeys.has(scheduler.scheduler_key)
                      }
                      requestToggle={actions.requestToggleScheduler}
                    />
                    {actionError && (
                      <p className="m-0 text-xs text-danger" role="alert">
                        {actionError}
                      </p>
                    )}
                  </div>
                </article>
              )
            })}
          </div>
        ))}
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

export function OperationsOverviewPage({ role }: { role?: AdminRole }) {
  const effectiveRole = role ?? "viewer"
  const audit: DashboardRequest["audit"] = {
    ...OPERATIONS_OVERVIEW_AUDIT,
  }
  const query = useOperationsDashboardQuery("overview", audit)
  const state = useOperationsDashboardState(query)
  const overview = state.response?.view === "overview" ? state.response : null
  const freshnessError = state.errors[0] ?? ""
  const schedulersError = state.errors[2] ?? ""
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
