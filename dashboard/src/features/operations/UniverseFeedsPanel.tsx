import { useQuery, useQueryClient } from "@tanstack/react-query"
import { useServerFn } from "@tanstack/react-start"
import type { ColumnDef } from "@tanstack/react-table"
import { ListChecks } from "lucide-react"
import { useMemo, useState } from "react"
import { QueryStatus } from "../../components/AsyncState"
import {
  liveQueryOptions,
  useOperationsRefresh,
  useRegisterOperationsQuery,
} from "../../components/OperationsRefresh"
import { useProtectedQueryScope } from "../../components/ProtectedQueryScope"
import { DataTable } from "../../components/data-table"
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
import { Badge } from "../../components/ui/badge"
import { Button } from "../../components/ui/button"
import { Input } from "../../components/ui/input"
import { Label } from "../../components/ui/label"
import { toast } from "../../components/ui/toast"
import type { UniverseFeed, UniverseRefresh } from "../../lib/admin-api"
import type { AdminRole } from "../../lib/admin-governance-api"
import {
  loadUniverseFeeds,
  requestUniverseRefresh,
} from "../../lib/admin.functions"
import { formatDate, Panel } from "./operations.shared"

const NOTE_MIN_LENGTH = 10
const NOTE_MAX_LENGTH = 500

function refreshInProgress(refresh: UniverseRefresh | null) {
  return refresh !== null && refresh.completed_at === null
}

function RefreshState({ refresh }: { refresh: UniverseRefresh | null }) {
  if (!refresh) return <span className="text-muted">—</span>
  const label =
    refresh.state === "queued"
      ? "排隊中"
      : refresh.state === "running"
        ? "執行中"
        : refresh.state === "completed"
          ? "已完成"
          : refresh.state === "failed"
            ? "失敗"
            : refresh.state === "cancelled"
              ? "已取消"
              : refresh.state
  return (
    <div className="space-y-1">
      <Badge
        variant={
          refresh.state === "failed"
            ? "destructive"
            : refresh.completed_at === null
              ? "warning"
              : "secondary"
        }
      >
        {label}
      </Badge>
      <p className="text-xs text-muted">
        {refresh.requested_by} · {formatDate(refresh.requested_at)}
      </p>
      {refresh.failure_code ? (
        <p className="font-mono text-xs wrap-anywhere text-danger">
          {refresh.failure_code}
        </p>
      ) : null}
    </div>
  )
}

function RefreshConfirmationDialog({
  feed,
  note,
  onNoteChange,
  onCancel,
  onConfirm,
}: {
  feed: UniverseFeed | null
  note: string
  onNoteChange: (value: string) => void
  onCancel: () => void
  onConfirm: () => void
}) {
  const noteValid = note.trim().length >= NOTE_MIN_LENGTH
  return (
    <AlertDialog
      open={feed !== null}
      onOpenChange={open => {
        if (!open) onCancel()
      }}
    >
      {feed && (
        <AlertDialogContent className="max-h-[calc(100dvh-2rem)] overflow-y-auto">
          <AlertDialogHeader>
            <AlertDialogTitle>確認手動更新名單？</AlertDialogTitle>
            <AlertDialogDescription>
              將為 {feed.provider} 的「{feed.dataset_key}
              」排入一次名單更新。手動更新等同核准：這次取得的名單會直接發布，不受
              2% 變更門檻限制，發布者記為你；尚無 baseline 時會成為第一份
              baseline。
            </AlertDialogDescription>
          </AlertDialogHeader>
          <ul className="my-0 list-disc space-y-1 pl-5 text-xs text-muted">
            <li>更新依佇列順序執行，排在已排隊的工作後面。</li>
            <li>排程停止時也會執行，只需要該來源的 Fetcher 在運作。</li>
            {feed.candidate ? (
              <li>
                目前有待發布的候選（生效日 {feed.candidate.effective_date}
                ，變更 {feed.candidate.change_count}{" "}
                筆）；名單內容相同時會直接發布它。
              </li>
            ) : null}
          </ul>
          <div className="space-y-1.5">
            <Label htmlFor="universe-refresh-note">更新原因</Label>
            <Input
              id="universe-refresh-note"
              value={note}
              maxLength={NOTE_MAX_LENGTH}
              placeholder="例如：已核對交易所公告的新上市商品"
              aria-describedby="universe-refresh-note-hint"
              onChange={event => onNoteChange(event.target.value)}
            />
            <p id="universe-refresh-note-hint" className="text-xs text-muted">
              至少 {NOTE_MIN_LENGTH} 個字，會記錄在發布的名單與 audit。
            </p>
          </div>
          <AlertDialogFooter>
            <AlertDialogCancel onClick={onCancel}>取消</AlertDialogCancel>
            <AlertDialogAction disabled={!noteValid} onClick={onConfirm}>
              確認更新並核准
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      )}
    </AlertDialog>
  )
}

export function UniverseFeedsPanel({
  role = "viewer",
  active = true,
}: {
  role?: AdminRole
  active?: boolean
}) {
  const loadFeeds = useServerFn(loadUniverseFeeds)
  const requestRefresh = useServerFn(requestUniverseRefresh)
  const scope = useProtectedQueryScope()
  const queryClient = useQueryClient()
  const { reportError } = useOperationsRefresh()
  const [target, setTarget] = useState<UniverseFeed | null>(null)
  const [note, setNote] = useState("")
  const [pendingKey, setPendingKey] = useState<string>()
  const queryKey = ["operations", "universe-feeds", scope] as const
  useRegisterOperationsQuery(queryKey, active)
  const feeds = useQuery({
    ...liveQueryOptions,
    queryKey,
    enabled: active,
    queryFn: () => loadFeeds(),
  })

  async function refresh(feed: UniverseFeed, reason: string) {
    const key = `${feed.provider}:${feed.dataset_key}`
    setPendingKey(key)
    try {
      await requestRefresh({
        data: {
          provider: feed.provider,
          datasetKey: feed.dataset_key,
          note: reason,
        },
      })
      toast.success(`已排入 ${feed.dataset_key} 的名單更新`, {
        description: "完成後名單會直接發布；進度顯示在「手動更新」欄。",
      })
      await Promise.all([
        queryClient.invalidateQueries({
          queryKey: ["operations", "universe-feeds"],
        }),
        queryClient.invalidateQueries({
          queryKey: ["operations", "universe-candidates"],
        }),
      ])
    } catch (reason) {
      if (reportError(reason)) return
      toast.error("名單更新排入失敗", {
        description: reason instanceof Error ? reason.message : String(reason),
      })
    } finally {
      setPendingKey(undefined)
    }
  }

  const columns = useMemo<ColumnDef<UniverseFeed, unknown>[]>(
    () => [
      {
        accessorKey: "dataset_key",
        header: "資料集",
        meta: { minWidth: 170, pin: "left" },
        cell: context => (
          <span className="font-mono wrap-anywhere">
            {context.getValue<string>()}
          </span>
        ),
      },
      { accessorKey: "provider", header: "來源", meta: { minWidth: 110 } },
      {
        id: "published",
        header: "目前名單",
        meta: { minWidth: 220 },
        cell: ({ row }) => {
          const published = row.original.published
          if (!published)
            return <span className="text-muted">尚未發布 baseline</span>
          return (
            <div className="space-y-1">
              <p>
                生效日 {published.effective_date} · {published.member_count} 檔
              </p>
              <p className="text-xs text-muted">
                {formatDate(published.published_at)} ·{" "}
                {published.published_by ?? "—"}
              </p>
            </div>
          )
        },
      },
      {
        id: "candidate",
        header: "待發布候選",
        meta: { minWidth: 170 },
        cell: ({ row }) => {
          const candidate = row.original.candidate
          if (!candidate) return <span className="text-muted">無</span>
          return (
            <Badge variant="warning">
              {candidate.effective_date} · 變更 {candidate.change_count}（
              {(candidate.change_ratio * 100).toFixed(2)}%）
            </Badge>
          )
        },
      },
      {
        id: "refresh",
        header: "手動更新",
        meta: { minWidth: 210 },
        cell: ({ row }) => <RefreshState refresh={row.original.refresh} />,
      },
      {
        id: "action",
        header: "操作",
        meta: { width: 130 },
        cell: ({ row }) => {
          const feed = row.original
          const key = `${feed.provider}:${feed.dataset_key}`
          if (role !== "owner")
            return <span className="text-xs text-muted">僅 owner 可更新</span>
          return (
            <Button
              variant="outline"
              size="sm"
              type="button"
              aria-label={`手動更新 ${feed.dataset_key} 名單`}
              disabled={
                pendingKey !== undefined || refreshInProgress(feed.refresh)
              }
              onClick={() => {
                setNote("")
                setTarget(feed)
              }}
            >
              {pendingKey === key ? "排入中…" : "手動更新"}
            </Button>
          )
        },
      },
    ],
    [pendingKey, role]
  )
  return (
    <Panel
      className="mb-5"
      title="Universe 名單"
      eyebrow="Governance"
      icon={<ListChecks size={19} />}
      result={{ ok: true, data: null }}
    >
      <p className="mb-3 text-sm text-muted">
        名單在每天的排程時刻自動更新；變更超過 2% 時保留為候選。Owner
        可以手動更新，手動更新取得的名單視同已核准，會直接發布。
      </p>
      <QueryStatus
        hasData={feeds.data !== undefined}
        pending={feeds.isFetching}
        error={feeds.error}
        updatedAt={feeds.dataUpdatedAt}
        onRetry={() => void feeds.refetch({ cancelRefetch: false })}
        label="Universe 名單"
      />
      <DataTable
        ariaLabel="Universe 名單"
        columns={columns}
        data={feeds.data?.data ?? []}
        getRowId={row => `${row.provider}:${row.dataset_key}`}
        isLoading={feeds.isPending}
        isRefreshing={feeds.isFetching && !feeds.isPending}
        refreshingState={null}
        error={feeds.data === undefined ? feeds.error : undefined}
        errorState={null}
        emptyState="目前沒有可更新名單的資料集。"
        fillAvailableWidth
      />
      <RefreshConfirmationDialog
        feed={target}
        note={note}
        onNoteChange={setNote}
        onCancel={() => setTarget(null)}
        onConfirm={() => {
          const feed = target
          const reason = note.trim()
          setTarget(null)
          if (feed) void refresh(feed, reason)
        }}
      />
    </Panel>
  )
}
