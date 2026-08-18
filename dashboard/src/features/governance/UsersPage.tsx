import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { useServerFn } from "@tanstack/react-start"
import { KeyRound, Plus, RefreshCw, UserCheck, UserX } from "lucide-react"
import { type FormEvent, useMemo, useState } from "react"
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
import type { AdminRole, AdminUser } from "../../lib/admin-governance-api"
import {
  createUser,
  loadUsers,
  resetUserPassword,
  updateUser,
} from "../../lib/admin-governance.functions"
import { SecretDialog } from "./SecretDialog"

function formatDate(value?: string | null) {
  if (!value) return "—"
  return new Intl.DateTimeFormat("zh-TW", {
    dateStyle: "medium",
    timeStyle: "short",
  }).format(new Date(value))
}

export function UsersPage() {
  const load = useServerFn(loadUsers)
  const create = useServerFn(createUser)
  const update = useServerFn(updateUser)
  const resetPassword = useServerFn(resetUserPassword)
  const queryClient = useQueryClient()
  const sessionScope = useProtectedQueryScope()
  const usersKey = ["admin", sessionScope, "users"] as const
  const usersQuery = useQuery({
    queryKey: usersKey,
    queryFn: () => load(),
  })
  const createMutation = useMutation({
    mutationFn: (variables: Parameters<typeof create>[0]) => create(variables),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: usersKey })
    },
  })
  const updateMutation = useMutation({
    mutationFn: (variables: Parameters<typeof update>[0]) => update(variables),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: usersKey })
    },
  })
  const resetMutation = useMutation({
    mutationFn: (variables: Parameters<typeof resetPassword>[0]) =>
      resetPassword(variables),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: usersKey })
    },
  })
  const [username, setUsername] = useState("")
  const [displayName, setDisplayName] = useState("")
  const [role, setRole] = useState<AdminRole>("viewer")
  const [password, setPassword] = useState("")
  const [secret, setSecret] = useState<{ title: string; value: string } | null>(
    null
  )

  async function submit(event: FormEvent) {
    event.preventDefault()
    try {
      const body = {
        username,
        display_name: displayName,
        role,
        must_change_password: true,
        ...(password ? { password } : {}),
      }
      const result = await createMutation.mutateAsync({
        data: body,
      })
      if (result.temporary_password) {
        setSecret({
          title: `${result.data.username} 的臨時密碼`,
          value: result.temporary_password,
        })
      }
      setUsername("")
      setDisplayName("")
      setPassword("")
      toast.success("使用者已建立", {
        description: `已建立 ${result.data.username}。`,
      })
    } catch (reason) {
      toast.error("建立使用者失敗", {
        description:
          reason instanceof Error ? reason.message : "建立使用者失敗。",
      })
    }
  }

  async function changeRole(user: AdminUser, nextRole: AdminRole) {
    try {
      await updateMutation.mutateAsync({
        data: { userId: user.user_id, role: nextRole },
      })
      toast.success("角色已更新", {
        description: `${user.username} 已改為 ${nextRole}。`,
      })
    } catch (reason) {
      toast.error("角色更新失敗", {
        description:
          reason instanceof Error ? reason.message : "角色更新失敗。",
      })
    }
  }

  async function toggleActive(user: AdminUser) {
    const action = user.is_active ? "停用" : "啟用"
    if (!window.confirm(`確定${action}「${user.username}」？`)) return
    try {
      await updateMutation.mutateAsync({
        data: { userId: user.user_id, is_active: !user.is_active },
      })
      toast.success(`使用者已${action}`, {
        description: `${user.username} 已${action}。`,
      })
    } catch (reason) {
      toast.error(`${action}使用者失敗`, {
        description:
          reason instanceof Error ? reason.message : `${action}失敗。`,
      })
    }
  }

  async function reset(user: AdminUser) {
    if (!window.confirm(`確定重設「${user.username}」的密碼？`)) return
    try {
      const result = await resetMutation.mutateAsync({
        data: { userId: user.user_id },
      })
      if (result.temporary_password) {
        setSecret({
          title: `${result.data.username} 的臨時密碼`,
          value: result.temporary_password,
        })
      }
      toast.success("密碼已重設", {
        description: `${user.username} 下次登入時須更改密碼。`,
      })
    } catch (reason) {
      toast.error("密碼重設失敗", {
        description:
          reason instanceof Error ? reason.message : "密碼重設失敗。",
      })
    }
  }

  const users = usersQuery.data?.data ?? []
  const userColumns = useMemo<ColumnDef<AdminUser, unknown>[]>(
    () => [
      {
        id: "identity",
        accessorKey: "username",
        header: "使用者",
        meta: { minWidth: 180 },
        cell: ({ row }) => (
          <>
            <strong className="block">{row.original.display_name}</strong>
            <span className="font-mono text-xs text-muted">
              {row.original.username}
            </span>
          </>
        ),
      },
      {
        accessorKey: "role",
        header: "角色",
        meta: { width: 140 },
        cell: ({ row }) => {
          const user = row.original
          const rolePending =
            updateMutation.isPending &&
            updateMutation.variables?.data.userId === user.user_id &&
            "role" in updateMutation.variables.data
          return (
            <select
              aria-label={`${user.username} 的角色`}
              className="h-9 rounded-lg border border-line bg-surface px-2 text-sm"
              value={user.role}
              disabled={rolePending}
              onChange={event =>
                void changeRole(user, event.target.value as AdminRole)
              }
            >
              <option value="owner">Owner</option>
              <option value="operator">Operator</option>
              <option value="viewer">Viewer</option>
            </select>
          )
        },
      },
      {
        id: "status",
        accessorFn: row => row.is_active,
        header: "狀態",
        meta: { minWidth: 150 },
        cell: ({ row }) => (
          <>
            <Badge variant={row.original.is_active ? "default" : "secondary"}>
              {row.original.is_active ? "active" : "inactive"}
            </Badge>
            {row.original.must_change_password && (
              <Badge className="ml-1" variant="warning">
                須改密碼
              </Badge>
            )}
          </>
        ),
      },
      {
        accessorKey: "last_login_at",
        header: "最後登入",
        meta: { minWidth: 168 },
        cell: ({ row }) => formatDate(row.original.last_login_at),
      },
      {
        id: "actions",
        header: "操作",
        enableSorting: false,
        meta: { width: 104, pin: "right", align: "right" },
        cell: ({ row }) => {
          const user = row.original
          const resetPending =
            resetMutation.isPending &&
            resetMutation.variables?.data.userId === user.user_id
          const activePending =
            updateMutation.isPending &&
            updateMutation.variables?.data.userId === user.user_id &&
            "is_active" in updateMutation.variables.data
          return (
            <div className="flex justify-end gap-1">
              <Button
                type="button"
                size="icon"
                variant="ghost"
                aria-label={`重設 ${user.username} 的密碼`}
                disabled={resetPending}
                onClick={() => void reset(user)}
              >
                <KeyRound
                  className={resetPending ? "animate-pulse" : ""}
                  size={16}
                />
              </Button>
              <Button
                type="button"
                size="icon"
                variant="ghost"
                aria-label={`${user.is_active ? "停用" : "啟用"} ${user.username}`}
                disabled={activePending}
                onClick={() => void toggleActive(user)}
              >
                {user.is_active ? <UserX size={16} /> : <UserCheck size={16} />}
              </Button>
            </div>
          )
        },
      },
    ],
    [
      resetMutation.isPending,
      resetMutation.variables,
      updateMutation.isPending,
      updateMutation.variables,
    ]
  )

  return (
    <div className="grid gap-5">
      <header>
        <p className="mb-1 font-mono text-xs font-medium tracking-widest text-accent uppercase">
          Admin identities
        </p>
        <h2 className="text-2xl font-bold tracking-tight sm:text-3xl">
          管理者使用者
        </h2>
        <p className="mt-2 text-sm text-muted">
          建立具名帳號、分配角色，並管理啟用狀態與臨時密碼。
        </p>
      </header>

      {usersQuery.error && users.length === 0 && (
        <Alert variant="destructive">
          <AlertTitle>無法載入使用者</AlertTitle>
          <AlertDescription>
            {usersQuery.error instanceof Error
              ? usersQuery.error.message
              : "無法載入使用者。"}
          </AlertDescription>
        </Alert>
      )}

      {usersQuery.error && users.length > 0 && (
        <Alert variant="destructive">
          <AlertTitle>更新使用者失敗</AlertTitle>
          <AlertDescription className="flex flex-wrap items-center justify-between gap-3">
            <span>
              {usersQuery.error instanceof Error
                ? usersQuery.error.message
                : "無法載入使用者。"}
            </span>
            <Button
              type="button"
              variant="outline"
              onClick={() => void usersQuery.refetch()}
            >
              <RefreshCw />
              重新載入
            </Button>
          </AlertDescription>
        </Alert>
      )}

      <Card className="gap-0 p-5">
        <CardHeader className="mb-4 px-0">
          <CardTitle className="flex items-center gap-2 text-lg">
            <Plus size={18} /> 建立使用者
          </CardTitle>
        </CardHeader>
        <CardContent className="px-0">
          <form
            className="grid gap-3 md:grid-cols-2 xl:grid-cols-5"
            autoComplete="off"
            onSubmit={submit}
          >
            <div className="grid gap-1.5">
              <Label htmlFor="new-username">Username</Label>
              <Input
                id="new-username"
                value={username}
                onChange={event => setUsername(event.target.value)}
                autoComplete="off"
                required
              />
            </div>
            <div className="grid gap-1.5">
              <Label htmlFor="new-display-name">顯示名稱</Label>
              <Input
                id="new-display-name"
                value={displayName}
                onChange={event => setDisplayName(event.target.value)}
                required
              />
            </div>
            <div className="grid gap-1.5">
              <Label htmlFor="new-role">角色</Label>
              <select
                id="new-role"
                className="h-10 rounded-lg border border-line bg-surface px-3 text-sm"
                value={role}
                onChange={event => setRole(event.target.value as AdminRole)}
              >
                <option value="owner">Owner</option>
                <option value="operator">Operator</option>
                <option value="viewer">Viewer</option>
              </select>
            </div>
            <div className="grid gap-1.5">
              <Label htmlFor="new-password">指定密碼（留空自動產生）</Label>
              <Input
                id="new-password"
                type="password"
                value={password}
                onChange={event => setPassword(event.target.value)}
                autoComplete="new-password"
              />
            </div>
            <div className="flex items-end">
              <Button
                className="w-full"
                type="submit"
                disabled={createMutation.isPending}
              >
                <Plus size={17} />{" "}
                {createMutation.isPending ? "處理中…" : "建立"}
              </Button>
            </div>
          </form>
        </CardContent>
      </Card>

      <Card className="gap-0 p-5">
        <CardHeader className="mb-4 flex grid-cols-none flex-row items-center justify-between px-0">
          <CardTitle className="text-lg">目前使用者</CardTitle>
          <Button
            type="button"
            variant="secondary"
            disabled={usersQuery.isFetching}
            onClick={() => {
              void usersQuery.refetch()
            }}
          >
            <RefreshCw
              className={usersQuery.isFetching ? "animate-spin" : ""}
              size={16}
            />
            更新
          </Button>
        </CardHeader>
        <CardContent className="px-0">
          <DataTable
            ariaLabel="目前使用者"
            caption="目前使用者"
            columns={userColumns}
            data={users}
            emptyState="目前沒有管理者使用者。"
            error={users.length === 0 ? usersQuery.error : undefined}
            errorState={
              usersQuery.error instanceof Error
                ? usersQuery.error.message
                : "無法載入使用者。"
            }
            fillAvailableWidth
            getRowId={user => user.user_id}
            isLoading={usersQuery.isPending}
            isRefreshing={usersQuery.isFetching && !usersQuery.isPending}
            loadingState={
              <div className="grid gap-2">
                <span className="sr-only">正在載入使用者</span>
                <Skeleton className="h-10" />
                <Skeleton className="h-10" />
                <Skeleton className="h-10" />
              </div>
            }
            tableClassName="min-w-[720px]"
          />
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
