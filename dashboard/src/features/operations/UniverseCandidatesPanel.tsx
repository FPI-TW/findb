import { useQuery } from "@tanstack/react-query"
import { useServerFn } from "@tanstack/react-start"
import type { ColumnDef } from "@tanstack/react-table"
import { GitCompareArrows } from "lucide-react"
import { useEffect, useMemo, useState } from "react"
import { QueryStatus } from "../../components/AsyncState"
import {
  liveQueryOptions,
  useRegisterOperationsQuery,
} from "../../components/OperationsRefresh"
import { useProtectedQueryScope } from "../../components/ProtectedQueryScope"
import { DataTable } from "../../components/data-table"
import { Button } from "../../components/ui/button"
import type { UniverseCandidate, UniverseDiff } from "../../lib/admin-api"
import {
  loadUniverseCandidates,
  loadUniverseDiff,
} from "../../lib/admin.functions"
import { formatDate, Panel } from "./operations.shared"

function MemberList({
  title,
  members,
}: {
  title: string
  members: Array<{ member_key: string }>
}) {
  return (
    <section className="rounded-xl border border-line bg-canvas/40 p-3">
      <h4 className="text-sm font-semibold text-ink">
        {title} <span className="text-muted">({members.length})</span>
      </h4>
      {members.length ? (
        <ul className="mt-2 max-h-56 space-y-1 overflow-auto text-xs">
          {members.map(member => (
            <li key={member.member_key} className="font-mono wrap-anywhere">
              {member.member_key}
            </li>
          ))}
        </ul>
      ) : (
        <p className="mt-2 text-xs text-muted">無</p>
      )}
    </section>
  )
}

function UniverseDiffDetails({ diff }: { diff: UniverseDiff }) {
  return (
    <div className="mt-4 rounded-xl border border-line bg-surface p-4">
      <div className="flex flex-wrap items-start justify-between gap-2">
        <div>
          <p className="text-xs font-medium tracking-[0.14em] text-muted uppercase">
            Member diff
          </p>
          <h3 className="mt-1 font-mono text-sm font-semibold wrap-anywhere">
            {diff.release.release_id}
          </h3>
        </div>
        <p className="text-xs text-muted">
          比較基準：
          {diff.comparison_release?.release_id ?? "無已發布基準"}
        </p>
      </div>
      <div className="mt-4 grid gap-3 lg:grid-cols-3">
        <MemberList title="新增" members={diff.added} />
        <MemberList title="移除" members={diff.removed} />
        <MemberList title="欄位變更" members={diff.changed} />
      </div>
      {diff.changed.length ? (
        <div className="mt-3 space-y-2">
          {diff.changed.map(change => (
            <div
              key={change.member_key}
              className="rounded-lg border border-line px-3 py-2 text-xs"
            >
              <p className="font-mono font-medium wrap-anywhere">
                {change.member_key}
              </p>
              <p className="mt-1 text-muted">
                變更欄位：{change.changed_fields.join("、")}
              </p>
            </div>
          ))}
        </div>
      ) : null}
      <p className="mt-3 text-xs text-muted">
        未變更成員：{diff.unchanged_count}
      </p>
    </div>
  )
}

export function UniverseCandidatesPanel({
  active = true,
}: {
  active?: boolean
}) {
  const loadCandidates = useServerFn(loadUniverseCandidates)
  const loadDiff = useServerFn(loadUniverseDiff)
  const scope = useProtectedQueryScope()
  const [page, setPage] = useState(1)
  const [pageSize, setPageSize] = useState(25)
  const [selectedReleaseId, setSelectedReleaseId] = useState<string>()
  const request = {
    datasetKey: "",
    provider: "",
    effectiveDate: "",
    page,
    pageSize,
  }
  const queryKey = [
    "operations",
    "universe-candidates",
    scope,
    request,
  ] as const
  const diffKey = [
    "operations",
    "universe-diff",
    scope,
    selectedReleaseId,
  ] as const
  useRegisterOperationsQuery(queryKey, active)
  useRegisterOperationsQuery(diffKey, active && selectedReleaseId !== undefined)
  const candidates = useQuery({
    ...liveQueryOptions,
    queryKey,
    enabled: active,
    queryFn: () => loadCandidates({ data: request }),
  })
  const diff = useQuery({
    ...liveQueryOptions,
    queryKey: diffKey,
    enabled: active && selectedReleaseId !== undefined,
    queryFn: () => {
      if (!selectedReleaseId) throw new Error("尚未選取 universe release")
      return loadDiff({ data: { releaseId: selectedReleaseId } })
    },
  })
  const pagination = candidates.data?.pagination
  useEffect(() => {
    if (!active || !pagination) return
    const nextPage = Math.min(page, Math.max(pagination.total_pages, 1))
    if (nextPage !== page) setPage(nextPage)
  }, [active, page, pagination])
  const columns = useMemo<ColumnDef<UniverseCandidate, unknown>[]>(
    () => [
      {
        accessorKey: "dataset_key",
        header: "資料集",
        meta: { minWidth: 180, pin: "left" },
        cell: context => (
          <span className="font-mono wrap-anywhere">
            {context.getValue<string>()}
          </span>
        ),
      },
      { accessorKey: "provider", header: "來源", meta: { minWidth: 110 } },
      {
        accessorKey: "effective_date",
        header: "生效日",
        meta: { minWidth: 120 },
      },
      {
        accessorKey: "change_count",
        header: "變更",
        meta: { minWidth: 130 },
        cell: ({ row }) =>
          `${row.original.change_count}（${(row.original.change_ratio * 100).toFixed(2)}%）`,
      },
      {
        accessorKey: "created_at",
        header: "建立時間",
        meta: { minWidth: 190 },
        cell: context => formatDate(context.getValue<string>()),
      },
      {
        id: "diff",
        header: "差異",
        meta: { width: 120 },
        cell: ({ row }) => (
          <Button
            variant="outline"
            size="sm"
            type="button"
            aria-label={`檢視 ${row.original.release_id} 成員差異`}
            onClick={() => setSelectedReleaseId(row.original.release_id)}
          >
            檢視差異
          </Button>
        ),
      },
    ],
    []
  )
  return (
    <Panel
      title="待發布 Universe 候選"
      eyebrow="Governance"
      icon={<GitCompareArrows size={19} />}
      result={{ ok: true, data: null }}
    >
      <p className="mb-3 text-sm text-muted">
        唯讀列出尚未發布的
        universe；選取候選後可與最近的已發布版本比較成員差異。
      </p>
      <QueryStatus
        hasData={candidates.data !== undefined}
        pending={candidates.isFetching}
        error={candidates.error}
        updatedAt={candidates.dataUpdatedAt}
        onRetry={() => void candidates.refetch({ cancelRefetch: false })}
        label="Universe 候選"
      />
      <DataTable
        ariaLabel="待發布 Universe 候選"
        columns={columns}
        data={candidates.data?.data ?? []}
        getRowId={row => row.release_id}
        isLoading={candidates.isPending}
        isRefreshing={candidates.isFetching && !candidates.isPending}
        refreshingState={null}
        error={candidates.data === undefined ? candidates.error : undefined}
        errorState={null}
        emptyState="目前沒有待發布的 universe 候選。"
        fillAvailableWidth
        manualPagination
        pageCount={Math.max(candidates.data?.pagination.total_pages ?? 1, 1)}
        rowCount={candidates.data?.pagination.total_records ?? 0}
        pagination={{ pageIndex: page - 1, pageSize }}
        pageSizeOptions={[25, 50, 100]}
        paginationAriaLabel="Universe 候選分頁"
        onPaginationChange={next => {
          const value =
            typeof next === "function"
              ? next({ pageIndex: page - 1, pageSize })
              : next
          setPage(value.pageSize === pageSize ? value.pageIndex + 1 : 1)
          setPageSize(value.pageSize)
        }}
      />
      {selectedReleaseId ? (
        <>
          <QueryStatus
            hasData={diff.data !== undefined}
            pending={diff.isFetching}
            error={diff.error}
            updatedAt={diff.dataUpdatedAt}
            onRetry={() => void diff.refetch({ cancelRefetch: false })}
            label="Universe 成員差異"
          />
          {diff.data ? <UniverseDiffDetails diff={diff.data} /> : null}
        </>
      ) : null}
    </Panel>
  )
}
