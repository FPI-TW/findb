import { useServerFn } from "@tanstack/react-start"
import { KeyRound, Plus, RefreshCw, RotateCw, Trash2 } from "lucide-react"
import { type FormEvent, useCallback, useEffect, useState } from "react"

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
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "../../components/ui/table"
import { toast } from "../../components/ui/toast"
import type {
  AdminRole,
  Credential,
  CredentialFilters,
  CredentialsOverview,
  SourceProvider,
} from "../../lib/admin-governance-api"
import {
  SOURCE_PROVIDERS,
  SOURCE_PROVIDER_DATASETS,
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
type SourceDatasetAccessMode = "all" | "selected"

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
    return `${sourceName} · 全部 datasets（含未來新增）`
  }
  return `${sourceName} · ${item.scopes.length} 個指定 datasets`
}

export function CredentialsPage({ role }: { role: AdminRole }) {
  const load = useServerFn(loadCredentials)
  const loadOverview = useServerFn(loadCredentialsOverview)
  const issue = useServerFn(issueCredential)
  const rotate = useServerFn(rotateCredential)
  const revoke = useServerFn(revokeCredential)
  const [credentials, setCredentials] = useState<Credential[]>([])
  const [overview, setOverview] = useState<CredentialsOverview | null>(null)
  const [filters, setFilters] = useState(EMPTY_FILTERS)
  const [draftFilters, setDraftFilters] = useState(EMPTY_FILTERS)
  const [loading, setLoading] = useState(true)
  const [pending, setPending] = useState(false)
  const [loadError, setLoadError] = useState("")
  const [secret, setSecret] = useState<{ title: string; value: string } | null>(
    null
  )
  const [kind, setKind] = useState<"source" | "serve" | "admin">("source")
  const [name, setName] = useState("")
  const [owner, setOwner] = useState("")
  const [description, setDescription] = useState("")
  const [sourceName, setSourceName] = useState<SourceProvider>("twelve_data")
  const [sourceDatasetAccessMode, setSourceDatasetAccessMode] =
    useState<SourceDatasetAccessMode>("selected")
  const [allowedDatasets, setAllowedDatasets] = useState<string[]>([])
  const [scopes, setScopes] = useState("")
  const [credentialRole, setCredentialRole] = useState<AdminRole>("operator")
  const [expiresAt, setExpiresAt] = useState("")

  const refresh = useCallback(
    async (nextFilters: CredentialFilters, initial = false) => {
      if (initial) setLoading(true)
      else setPending(true)
      setLoadError("")
      try {
        const [credentialResult, overviewResult] = await Promise.all([
          load({ data: nextFilters }),
          loadOverview(),
        ])
        setCredentials(credentialResult.data)
        setOverview(overviewResult)
      } catch (reason) {
        const message =
          reason instanceof Error ? reason.message : "無法載入 credential。"
        if (initial) setLoadError(message)
        else toast.error("更新 Credential 失敗", { description: message })
      } finally {
        setLoading(false)
        setPending(false)
      }
    },
    [load, loadOverview]
  )

  useEffect(() => {
    void refresh(EMPTY_FILTERS, true)
  }, [refresh])

  async function submitIssue(event: FormEvent) {
    event.preventDefault()
    setPending(true)
    const expiry = expiresAt ? new Date(expiresAt).toISOString() : undefined
    const split = (value: string) =>
      value
        .split(",")
        .map(item => item.trim())
        .filter(Boolean)
    try {
      const result =
        kind === "source"
          ? await issue({
              data: {
                kind,
                name,
                owner,
                description: description || undefined,
                source_name: sourceName,
                allowed_datasets:
                  sourceDatasetAccessMode === "all" ? null : allowedDatasets,
                expires_at: expiry,
              },
            })
          : kind === "serve"
            ? await issue({
                data: {
                  kind,
                  name: name || undefined,
                  owner,
                  description: description || undefined,
                  scopes: split(scopes),
                  expires_at: expiry,
                },
              })
            : await issue({
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
      setSourceDatasetAccessMode("selected")
      setAllowedDatasets([])
      setScopes("")
      setExpiresAt("")
      toast.success("Credential 已簽發", {
        description: `${result.data.name} 已建立，請立即保存密鑰。`,
      })
      await refresh(filters)
    } catch (reason) {
      toast.error("簽發 Credential 失敗", {
        description: reason instanceof Error ? reason.message : "簽發失敗。",
      })
      setPending(false)
    }
  }

  async function rotateItem(item: Credential) {
    if (!item.id || !canManageCredential(role, item.kind)) return
    setPending(true)
    try {
      const result = await rotate({ data: { kind: item.kind, id: item.id } })
      setSecret({
        title: `已輪替 ${result.data.name}`,
        value: result.api_key,
      })
      toast.success("Credential 已輪替", {
        description: `${result.data.name} 的新密鑰已產生，請立即保存。`,
      })
      await refresh(filters)
    } catch (reason) {
      toast.error("輪替 Credential 失敗", {
        description: reason instanceof Error ? reason.message : "輪替失敗。",
      })
      setPending(false)
    }
  }

  async function revokeItem(item: Credential) {
    if (
      !item.id ||
      !canManageCredential(role, item.kind) ||
      !window.confirm(`確定立即撤銷「${item.name}」？撤銷後下一個請求即失效。`)
    )
      return
    setPending(true)
    try {
      await revoke({ data: { kind: item.kind, id: item.id } })
      toast.success("Credential 已撤銷", {
        description: `${item.name} 已立即失效。`,
      })
      await refresh(filters)
    } catch (reason) {
      toast.error("撤銷 Credential 失敗", {
        description: reason instanceof Error ? reason.message : "撤銷失敗。",
      })
      setPending(false)
    }
  }

  const mayIssue = canIssueCredential(role, kind)

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

      {loadError && (
        <Alert variant="destructive">
          <AlertTitle>無法載入 Credential</AlertTitle>
          <AlertDescription>{loadError}</AlertDescription>
        </Alert>
      )}

      {loading ? (
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
                        setSourceName(
                          sourceProviderSchema.parse(event.target.value)
                        )
                        setAllowedDatasets([])
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
                    <div className="grid gap-2 sm:grid-cols-2">
                      <label className="flex items-start gap-2 rounded-md border border-line px-3 py-2 text-sm">
                        <input
                          aria-label="此 Provider 的全部 datasets（包含未來新增）"
                          checked={sourceDatasetAccessMode === "all"}
                          className="mt-0.5"
                          name="source-dataset-access-mode"
                          onChange={() => setSourceDatasetAccessMode("all")}
                          type="radio"
                        />
                        <span>
                          <strong className="block font-medium">
                            此 Provider 的全部 datasets
                          </strong>
                          <span className="text-xs text-muted">
                            包含未來新增的 datasets
                          </span>
                        </span>
                      </label>
                      <label className="flex items-start gap-2 rounded-md border border-line px-3 py-2 text-sm">
                        <input
                          aria-label="僅限指定 datasets"
                          checked={sourceDatasetAccessMode === "selected"}
                          className="mt-0.5"
                          name="source-dataset-access-mode"
                          onChange={() =>
                            setSourceDatasetAccessMode("selected")
                          }
                          type="radio"
                        />
                        <span>
                          <strong className="block font-medium">
                            僅限指定 datasets
                          </strong>
                          <span className="text-xs text-muted">
                            僅允許下方勾選的資料集
                          </span>
                        </span>
                      </label>
                    </div>
                    {sourceDatasetAccessMode === "selected" && (
                      <>
                        <div className="grid max-h-48 gap-2 overflow-y-auto sm:grid-cols-2 xl:grid-cols-3">
                          {SOURCE_PROVIDER_DATASETS[sourceName].map(dataset => (
                            <label
                              className="flex items-center gap-2 rounded-md border border-line px-3 py-2 text-sm"
                              key={dataset}
                            >
                              <input
                                aria-label={dataset}
                                checked={allowedDatasets.includes(dataset)}
                                onChange={event =>
                                  setAllowedDatasets(current =>
                                    event.target.checked
                                      ? [...current, dataset]
                                      : current.filter(item => item !== dataset)
                                  )
                                }
                                type="checkbox"
                              />
                              <span className="font-mono text-xs">
                                {dataset}
                              </span>
                            </label>
                          ))}
                        </div>
                        {allowedDatasets.length === 0 && (
                          <p className="text-xs text-warning">
                            指定模式下至少選擇一個 dataset，否則 credential
                            無法簽發。
                          </p>
                        )}
                      </>
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
                    pending ||
                    (kind === "source" &&
                      sourceDatasetAccessMode === "selected" &&
                      allowedDatasets.length === 0)
                  }
                >
                  <KeyRound size={17} /> {pending ? "處理中…" : "簽發"}
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
              setFilters(draftFilters)
              void refresh(draftFilters)
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
            <Button type="submit" disabled={pending}>
              <RefreshCw className={pending ? "animate-spin" : ""} size={17} />
              更新
            </Button>
          </form>
          {loading ? (
            <div className="grid gap-2" role="status">
              <span className="sr-only">正在載入 credential</span>
              <Skeleton className="h-10" />
              <Skeleton className="h-10" />
              <Skeleton className="h-10" />
            </div>
          ) : credentials.length === 0 ? (
            <Alert role="status">
              <AlertDescription>沒有符合條件的 credential。</AlertDescription>
            </Alert>
          ) : (
            <Table scrollMode="page">
              <TableHeader>
                <TableRow>
                  <TableHead>名稱 / 類型</TableHead>
                  <TableHead>Owner</TableHead>
                  <TableHead>狀態</TableHead>
                  <TableHead>Fingerprint</TableHead>
                  <TableHead>最後使用 / 次數</TableHead>
                  <TableHead>到期</TableHead>
                  <TableHead className="text-right">操作</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {credentials.map(item => (
                  <TableRow key={item.credential_ref}>
                    <TableCell>
                      <strong className="block">{item.name}</strong>
                      <span className="font-mono text-xs text-muted">
                        {item.kind}
                      </span>
                      {sourceScopeLabel(item) && (
                        <span className="mt-1 block text-xs text-muted">
                          {sourceScopeLabel(item)}
                        </span>
                      )}
                    </TableCell>
                    <TableCell>{item.owner ?? "—"}</TableCell>
                    <TableCell>
                      <Badge variant={statusVariant(item.status)}>
                        {item.status}
                      </Badge>
                    </TableCell>
                    <TableCell className="font-mono text-xs">
                      {item.fingerprint ?? "—"}
                    </TableCell>
                    <TableCell>
                      <span className="block">
                        {formatDate(item.last_used_at)}
                      </span>
                      <span className="text-xs text-muted">
                        {item.usage_count.toLocaleString()} requests
                      </span>
                    </TableCell>
                    <TableCell>{formatDate(item.expires_at)}</TableCell>
                    <TableCell>
                      {item.status !== "revoked" &&
                        item.id &&
                        canManageCredential(role, item.kind) && (
                          <div className="flex justify-end gap-1">
                            <Button
                              type="button"
                              size="icon"
                              variant="ghost"
                              aria-label={`輪替 ${item.name}`}
                              disabled={pending}
                              onClick={() => void rotateItem(item)}
                            >
                              <RotateCw size={16} />
                            </Button>
                            <Button
                              type="button"
                              size="icon"
                              variant="ghost"
                              aria-label={`撤銷 ${item.name}`}
                              disabled={pending}
                              onClick={() => void revokeItem(item)}
                            >
                              <Trash2 size={16} />
                            </Button>
                          </div>
                        )}
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          )}
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
