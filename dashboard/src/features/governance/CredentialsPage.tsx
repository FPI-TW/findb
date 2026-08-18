import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { useServerFn } from "@tanstack/react-start"
import { KeyRound, Plus, RefreshCw, RotateCw, Trash2 } from "lucide-react"
import { type FormEvent, useEffect, useMemo, useState } from "react"
import type { ColumnDef } from "@tanstack/react-table"

import { DataTable } from "../../components/data-table"
import { useProtectedQueryScope } from "../../components/ProtectedQueryScope"
import { Alert, AlertDescription, AlertTitle } from "../../components/ui/alert"
import { Badge } from "../../components/ui/badge"
import { Button } from "../../components/ui/button"
import {
  Card,
  CardContent,
  CardHeader,
  CardTitle,
} from "../../components/ui/card"
import { Input } from "../../components/ui/input"
import { Label } from "../../components/ui/label"
import { Skeleton } from "../../components/ui/skeleton"
import { toast } from "../../components/ui/toast"
import type {
  AdminRole,
  Credential,
  CredentialFilters,
  CredentialsOverview,
  CreateCredential,
  SourceProvider,
} from "../../lib/admin-governance-api"
import {
  SOURCE_PROVIDERS,
  SOURCE_PROVIDER_DATASETS,
  credentialFiltersSchema,
  credentialKindSchema,
  credentialStatusSchema,
  sourceProviderSchema,
} from "../../lib/admin-governance-api"
import {
  issueCredential,
  loadCredentials,
  loadCredentialsOverview,
  revokeCredential,
  rotateCredential,
} from "../../lib/admin-governance.functions"
import {
  canIssueCredential,
  canManageCredential,
} from "../../lib/admin-permissions"
import { SecretDialog } from "./SecretDialog"

const EMPTY_FILTERS: CredentialFilters = { kind: "", status: "", owner: "" }

type CredentialTarget = { kind: Credential["kind"]; id: string }

function formatDate(value: string | null) {
  if (!value) return "—"
  return new Intl.DateTimeFormat("zh-TW", {
    dateStyle: "medium",
    timeStyle: "short",
  }).format(new Date(value))
}

function statusVariant(status: Credential["status"]) {
  if (status === "active") return "default" as const
  if (status === "expiring") return "warning" as const
  if (status === "revoked" || status === "expired")
    return "destructive" as const
  return "secondary" as const
}

function sourceScopeLabel(item: Credential) {
  if (item.kind !== "source") return null
  const configuredSource = item.policies?.source_name
  const sourceName =
    typeof configuredSource === "string" ? configuredSource : "Provider"
  if (item.scopes === null) {
    return `${sourceName} · dataset scope 未設定（需重新簽發）`
  }
  return `${sourceName} · ${item.scopes.length} 個指定 datasets`
}

export function CredentialsPage({
  role,
  search,
  updateSearch,
}: {
  role: AdminRole
  search?: CredentialFilters
  updateSearch?: (next: CredentialFilters) => void
}) {
  const load = useServerFn(loadCredentials)
  const loadOverview = useServerFn(loadCredentialsOverview)
  const issue = useServerFn(issueCredential)
  const rotate = useServerFn(rotateCredential)
  const revoke = useServerFn(revokeCredential)
  const queryClient = useQueryClient()
  const sessionScope = useProtectedQueryScope()
  const credentialsRootKey = ["admin", sessionScope, "credentials"] as const
  const overviewKey = ["admin", sessionScope, "credentials-overview"] as const
  const [localFilters, setLocalFilters] = useState(EMPTY_FILTERS)
  const filters = search ?? localFilters
  const [draftFilters, setDraftFilters] = useState(filters)
  const [secret, setSecret] = useState<{ title: string; value: string } | null>(
    null
  )
  const [kind, setKind] = useState<"source" | "serve" | "admin">("source")
  const [name, setName] = useState("")
  const [owner, setOwner] = useState("")
  const [description, setDescription] = useState("")
  const [sourceName, setSourceName] = useState<SourceProvider>("twelve_data")
  const [allowedDatasets, setAllowedDatasets] = useState<string[]>([
    ...SOURCE_PROVIDER_DATASETS.twelve_data,
  ])
  const [scopes, setScopes] = useState("")
  const [credentialRole, setCredentialRole] = useState<AdminRole>("operator")
  const [expiresAt, setExpiresAt] = useState("")

  useEffect(() => {
    setDraftFilters(filters)
  }, [filters.kind, filters.owner, filters.status])

  const credentialsQuery = useQuery({
    queryKey: [...credentialsRootKey, filters],
    queryFn: () => load({ data: filters }),
  })
  const overviewQuery = useQuery({
    queryKey: overviewKey,
    queryFn: () => loadOverview(),
  })

  const invalidateCredentials = () => {
    void queryClient.invalidateQueries({ queryKey: credentialsRootKey })
    void queryClient.invalidateQueries({ queryKey: overviewKey })
  }

  const issueMutation = useMutation({
    mutationFn: (variables: { data: CreateCredential }) => issue(variables),
    onSuccess: invalidateCredentials,
  })
  const rotateMutation = useMutation({
    mutationFn: (target: CredentialTarget) => rotate({ data: target }),
    onSuccess: invalidateCredentials,
  })
  const revokeMutation = useMutation({
    mutationFn: (target: CredentialTarget) => revoke({ data: target }),
    onSuccess: invalidateCredentials,
  })

  function applyFilters(next: CredentialFilters) {
    setLocalFilters(next)
    updateSearch?.(next)
  }

  function refresh() {
    void credentialsQuery.refetch()
    void overviewQuery.refetch()
  }

  async function submitIssue(event: FormEvent) {
    event.preventDefault()
    const expiry = expiresAt ? new Date(expiresAt).toISOString() : undefined
    const split = (value: string) =>
      value
        .split(",")
        .map(item => item.trim())
        .filter(Boolean)
    try {
      const result =
        kind === "source"
          ? await issueMutation.mutateAsync({
              data: {
                kind,
                name,
                owner,
                description: description || undefined,
                source_name: sourceName,
                allowed_datasets: allowedDatasets,
                expires_at: expiry,
              },
            })
          : kind === "serve"
            ? await issueMutation.mutateAsync({
                data: {
                  kind,
                  name: name || undefined,
                  owner,
                  description: description || undefined,
                  tier: undefined,
                  scopes: split(scopes),
                  expires_at: expiry,
                },
              })
            : await issueMutation.mutateAsync({
                data: {
                  kind,
                  name: name || undefined,
                  owner,
                  description: description || undefined,
                  role: credentialRole,
                  scopes: split(scopes),
                  expires_at: expiry,
                },
              })
      setSecret({ title: `已簽發 ${result.data.name}`, value: result.api_key })
      setName("")
      setDescription("")
      setAllowedDatasets([...SOURCE_PROVIDER_DATASETS[sourceName]])
      setScopes("")
      setExpiresAt("")
      toast.success("Credential 已簽發", {
        description: `${result.data.name} 已建立，請立即保存密鑰。`,
      })
    } catch (reason) {
      toast.error("簽發 Credential 失敗", {
        description: reason instanceof Error ? reason.message : "簽發失敗。",
      })
    }
  }

  async function rotateItem(item: Credential) {
    if (!item.id || !canManageCredential(role, item.kind)) return
    try {
      const result = await rotateMutation.mutateAsync({
        kind: item.kind,
        id: item.id,
      })
      setSecret({
        title: `已輪替 ${result.data.name}`,
        value: result.api_key,
      })
      toast.success("Credential 已輪替", {
        description: `${result.data.name} 的新密鑰已產生，請立即保存。`,
      })
    } catch (reason) {
      toast.error("輪替 Credential 失敗", {
        description: reason instanceof Error ? reason.message : "輪替失敗。",
      })
    }
  }

  async function revokeItem(item: Credential) {
    if (
      !item.id ||
      !canManageCredential(role, item.kind) ||
      !window.confirm(`確定立即撤銷「${item.name}」？撤銷後下一個請求即失效。`)
    )
      return
    try {
      await revokeMutation.mutateAsync({ kind: item.kind, id: item.id })
      toast.success("Credential 已撤銷", {
        description: `${item.name} 已立即失效。`,
      })
    } catch (reason) {
      toast.error("撤銷 Credential 失敗", {
        description: reason instanceof Error ? reason.message : "撤銷失敗。",
      })
    }
  }

  const mayIssue = canIssueCredential(role, kind)
  const credentials = credentialsQuery.data?.data ?? []
  const overview: CredentialsOverview | undefined = overviewQuery.data
  const credentialColumns = useMemo<ColumnDef<Credential, unknown>[]>(
    () => [
      {
        id: "name",
        accessorKey: "name",
        header: "名稱 / 類型",
        meta: { minWidth: 180 },
        cell: ({ row }) => (
          <>
            <strong className="block">{row.original.name}</strong>
            <span className="font-mono text-xs text-muted">
              {row.original.kind}
            </span>
            {sourceScopeLabel(row.original) && (
              <span className="mt-1 block text-xs text-muted">
                {sourceScopeLabel(row.original)}
              </span>
            )}
          </>
        ),
      },
      {
        accessorKey: "owner",
        header: "Owner",
        meta: { width: 132 },
        cell: ({ row }) => row.original.owner ?? "—",
      },
      {
        accessorKey: "status",
        header: "狀態",
        meta: { width: 112 },
        cell: ({ row }) => (
          <Badge variant={statusVariant(row.original.status)}>
            {row.original.status}
          </Badge>
        ),
      },
      {
        accessorKey: "fingerprint",
        header: "Fingerprint",
        meta: { minWidth: 150 },
        cell: ({ row }) => (
          <span className="font-mono text-xs">
            {row.original.fingerprint ?? "—"}
          </span>
        ),
      },
      {
        id: "usage",
        accessorFn: row => row.last_used_at,
        header: "最後使用 / 次數",
        meta: { minWidth: 160 },
        cell: ({ row }) => (
          <>
            <span className="block">
              {formatDate(row.original.last_used_at)}
            </span>
            <span className="text-xs text-muted">
              {row.original.usage_count.toLocaleString()} requests
            </span>
          </>
        ),
      },
      {
        accessorKey: "expires_at",
        header: "到期",
        meta: { width: 168 },
        cell: ({ row }) => formatDate(row.original.expires_at),
      },
      {
        id: "actions",
        header: "操作",
        enableSorting: false,
        meta: {
          width: 112,
          fitContent: true,
          pin: "right",
          align: "right",
        },
        cell: ({ row }) => {
          const item = row.original
          if (
            item.status === "revoked" ||
            !item.id ||
            !canManageCredential(role, item.kind)
          ) {
            return null
          }
          const rotating =
            rotateMutation.isPending && rotateMutation.variables?.id === item.id
          const revoking =
            revokeMutation.isPending && revokeMutation.variables?.id === item.id
          return (
            <div className="flex justify-end gap-1">
              <Button
                type="button"
                size="icon"
                variant="ghost"
                aria-label={`輪替 ${item.name}`}
                disabled={rotating}
                onClick={() => void rotateItem(item)}
              >
                <RotateCw
                  className={rotating ? "animate-spin" : ""}
                  size={16}
                />
              </Button>
              <Button
                type="button"
                size="icon"
                variant="ghost"
                aria-label={`撤銷 ${item.name}`}
                disabled={revoking}
                onClick={() => void revokeItem(item)}
              >
                <Trash2 size={16} />
              </Button>
            </div>
          )
        },
      },
    ],
    [
      revokeMutation.isPending,
      revokeMutation.variables,
      role,
      rotateMutation.isPending,
      rotateMutation.variables,
    ]
  )
  const credentialsError = credentialsQuery.error
  const overviewError = overviewQuery.error

  return (
    <div className="grid gap-5">
      <header>
        <p className="mb-1 font-mono text-xs font-medium tracking-widest text-accent uppercase">
          Credential governance
        </p>
        <h2 className="text-2xl font-bold tracking-tight sm:text-3xl">
          API Credentials
        </h2>
        <p className="mt-2 text-sm text-muted">
          集中確認用途、owner、生命週期、使用狀態，並安全增發與輪替。
        </p>
      </header>

      {overviewError && (
        <Alert variant="destructive">
          <AlertTitle>無法載入 Credential 狀態</AlertTitle>
          <AlertDescription>
            {overviewError instanceof Error
              ? overviewError.message
              : "無法載入 credential 狀態。"}
          </AlertDescription>
        </Alert>
      )}

      {credentialsQuery.error && credentials.length > 0 && (
        <Alert variant="destructive">
          <AlertTitle>更新 Credential 失敗</AlertTitle>
          <AlertDescription className="flex flex-wrap items-center justify-between gap-3">
            <span>
              {credentialsQuery.error instanceof Error
                ? credentialsQuery.error.message
                : "無法載入 credential。"}
            </span>
            <Button
              type="button"
              variant="outline"
              onClick={() => void credentialsQuery.refetch()}
            >
              <RefreshCw />
              重新載入
            </Button>
          </AlertDescription>
        </Alert>
      )}

      {overviewQuery.isPending ? (
        <div className="grid gap-3 sm:grid-cols-5" role="status">
          <span className="sr-only">正在載入 credential 狀態</span>
          {Array.from({ length: 5 }).map((_, index) => (
            <Skeleton key={index} className="h-24 rounded-xl" />
          ))}
        </div>
      ) : overview ? (
        <div className="grid gap-3 sm:grid-cols-5">
          {Object.entries(overview.counts).map(([label, count]) => (
            <Card className="gap-1 p-4" key={label}>
              <span className="font-mono text-xs tracking-wide text-muted uppercase">
                {label}
              </span>
              <strong className="text-2xl">{count}</strong>
            </Card>
          ))}
        </div>
      ) : null}

      {mayIssue && (
        <Card className="gap-0 p-5">
          <CardHeader className="mb-4 px-0">
            <CardTitle className="flex items-center gap-2 text-lg">
              <Plus size={18} /> 簽發 credential
            </CardTitle>
          </CardHeader>
          <CardContent className="px-0">
            <form
              className="grid gap-3 md:grid-cols-2 xl:grid-cols-4"
              onSubmit={submitIssue}
            >
              <div className="grid gap-1.5">
                <Label htmlFor="credential-kind">類型</Label>
                <select
                  id="credential-kind"
                  className="h-10 rounded-lg border border-line bg-surface px-3 text-sm"
                  value={kind}
                  onChange={event =>
                    setKind(event.target.value as "source" | "serve" | "admin")
                  }
                >
                  <option value="source">Source</option>
                  <option value="serve">Serve</option>
                  {role === "owner" && <option value="admin">Admin</option>}
                </select>
              </div>
              <div className="grid gap-1.5">
                <Label htmlFor="credential-name">名稱</Label>
                <Input
                  id="credential-name"
                  value={name}
                  onChange={event => setName(event.target.value)}
                  required={kind === "source"}
                />
              </div>
              <div className="grid gap-1.5">
                <Label htmlFor="credential-owner">Owner</Label>
                <Input
                  id="credential-owner"
                  value={owner}
                  onChange={event => setOwner(event.target.value)}
                  required
                />
              </div>
              {kind === "source" ? (
                <>
                  <div className="grid gap-1.5">
                    <Label htmlFor="credential-source">Source name</Label>
                    <select
                      id="credential-source"
                      className="h-10 rounded-lg border border-line bg-surface px-3 text-sm"
                      value={sourceName}
                      onChange={event => {
                        const nextSource = sourceProviderSchema.parse(
                          event.target.value
                        )
                        setSourceName(nextSource)
                        setAllowedDatasets([
                          ...SOURCE_PROVIDER_DATASETS[nextSource],
                        ])
                      }}
                      required
                    >
                      {SOURCE_PROVIDERS.map(provider => (
                        <option key={provider} value={provider}>
                          {provider}
                        </option>
                      ))}
                    </select>
                  </div>
                  <fieldset className="grid gap-2 rounded-lg border border-line p-3 md:col-span-2 xl:col-span-3">
                    <legend className="px-1 text-sm font-medium">
                      Dataset 權限
                    </legend>
                    <p className="text-xs text-muted">
                      預設選取此 Provider 的完整治理清單；可縮小為至少一個
                      dataset。新增 dataset 需另行治理核准。
                    </p>
                    <div className="grid max-h-48 gap-2 overflow-y-auto sm:grid-cols-2 xl:grid-cols-3">
                      {SOURCE_PROVIDER_DATASETS[sourceName].map(dataset => (
                        <label
                          className="rounded-md border border-line px-3 py-2 text-sm"
                          key={dataset}
                        >
                          <input
                            aria-label={dataset}
                            checked={allowedDatasets.includes(dataset)}
                            className="mr-2"
                            onChange={event =>
                              setAllowedDatasets(current =>
                                event.target.checked
                                  ? [...current, dataset]
                                  : current.filter(item => item !== dataset)
                              )
                            }
                            type="checkbox"
                          />
                          <span className="font-mono text-xs">{dataset}</span>
                        </label>
                      ))}
                    </div>
                    {allowedDatasets.length === 0 && (
                      <p className="text-xs text-warning">
                        至少選擇一個 dataset，否則 credential 無法簽發。
                      </p>
                    )}
                  </fieldset>
                </>
              ) : (
                <div className="grid gap-1.5 md:col-span-2">
                  <Label htmlFor="credential-scopes">Scopes（逗號分隔）</Label>
                  <Input
                    id="credential-scopes"
                    value={scopes}
                    onChange={event => setScopes(event.target.value)}
                  />
                </div>
              )}
              {kind === "admin" && (
                <div className="grid gap-1.5">
                  <Label htmlFor="credential-role">角色</Label>
                  <select
                    id="credential-role"
                    className="h-10 rounded-lg border border-line bg-surface px-3 text-sm"
                    value={credentialRole}
                    onChange={event =>
                      setCredentialRole(event.target.value as AdminRole)
                    }
                  >
                    <option value="owner">Owner</option>
                    <option value="operator">Operator</option>
                    <option value="viewer">Viewer</option>
                  </select>
                </div>
              )}
              <div className="grid gap-1.5">
                <Label htmlFor="credential-expiry">到期時間（可選）</Label>
                <Input
                  id="credential-expiry"
                  type="datetime-local"
                  value={expiresAt}
                  onChange={event => setExpiresAt(event.target.value)}
                />
              </div>
              <div className="grid gap-1.5 md:col-span-2">
                <Label htmlFor="credential-description">說明</Label>
                <Input
                  id="credential-description"
                  value={description}
                  onChange={event => setDescription(event.target.value)}
                />
              </div>
              <div className="flex items-end">
                <Button
                  className="w-full"
                  type="submit"
                  disabled={
                    issueMutation.isPending ||
                    (kind === "source" && allowedDatasets.length === 0)
                  }
                >
                  <KeyRound size={17} />{" "}
                  {issueMutation.isPending ? "處理中…" : "簽發"}
                </Button>
              </div>
            </form>
          </CardContent>
        </Card>
      )}

      <Card className="gap-0 p-5">
        <CardHeader className="mb-4 px-0">
          <CardTitle className="text-lg">目前 Credentials</CardTitle>
        </CardHeader>
        <CardContent className="px-0">
          <form
            className="mb-4 grid gap-2 sm:grid-cols-4"
            onSubmit={event => {
              event.preventDefault()
              const nextFilters = credentialFiltersSchema.parse(draftFilters)
              applyFilters(nextFilters)
              if (
                nextFilters.kind === filters.kind &&
                nextFilters.status === filters.status &&
                nextFilters.owner === filters.owner
              ) {
                refresh()
              }
            }}
          >
            <select
              aria-label="Credential 類型"
              className="h-10 rounded-lg border border-line bg-surface px-3 text-sm"
              value={draftFilters.kind}
              onChange={event =>
                setDraftFilters({
                  ...draftFilters,
                  kind:
                    event.target.value === ""
                      ? ""
                      : credentialKindSchema.parse(event.target.value),
                })
              }
            >
              <option value="">所有類型</option>
              <option value="source">Source</option>
              <option value="serve">Serve</option>
              <option value="admin">Admin</option>
            </select>
            <select
              aria-label="Credential 狀態"
              className="h-10 rounded-lg border border-line bg-surface px-3 text-sm"
              value={draftFilters.status}
              onChange={event =>
                setDraftFilters({
                  ...draftFilters,
                  status:
                    event.target.value === ""
                      ? ""
                      : credentialStatusSchema.parse(event.target.value),
                })
              }
            >
              <option value="">所有狀態</option>
              <option value="active">Active</option>
              <option value="expiring">Expiring</option>
              <option value="expired">Expired</option>
              <option value="revoked">Revoked</option>
            </select>
            <Input
              aria-label="Credential owner"
              placeholder="Owner"
              value={draftFilters.owner}
              onChange={event =>
                setDraftFilters({ ...draftFilters, owner: event.target.value })
              }
            />
            <Button type="submit" disabled={credentialsQuery.isFetching}>
              <RefreshCw
                className={credentialsQuery.isFetching ? "animate-spin" : ""}
                size={17}
              />
              更新
            </Button>
          </form>
          <DataTable
            ariaLabel="目前 Credentials"
            caption="目前 Credentials"
            columns={credentialColumns}
            data={credentials}
            emptyState="沒有符合條件的 credential。"
            error={credentials.length === 0 ? credentialsError : undefined}
            errorState={
              credentialsError instanceof Error
                ? credentialsError.message
                : "無法載入 credential。"
            }
            fillAvailableWidth
            getRowId={item => item.credential_ref}
            isLoading={credentialsQuery.isPending}
            isRefreshing={
              credentialsQuery.isFetching && !credentialsQuery.isPending
            }
            loadingState={
              <div className="grid gap-2">
                <span className="sr-only">正在載入 credential</span>
                <Skeleton className="h-10" />
                <Skeleton className="h-10" />
                <Skeleton className="h-10" />
              </div>
            }
            tableClassName="min-w-[980px]"
          />
          {overview && (
            <p className="mt-3 text-xs text-muted">
              使用量為近即時估算；聚合更新：
              {formatDate(overview.usage_updated_at)}
            </p>
          )}
        </CardContent>
      </Card>
      {secret && (
        <SecretDialog
          title={secret.title}
          secret={secret.value}
          onClose={() => setSecret(null)}
        />
      )}
    </div>
  )
}
