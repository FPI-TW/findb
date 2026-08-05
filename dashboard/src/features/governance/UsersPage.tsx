import { useServerFn } from "@tanstack/react-start"
import { KeyRound, Plus, RefreshCw, UserCheck, UserX } from "lucide-react"
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
  const [users, setUsers] = useState<AdminUser[]>([])
  const [loading, setLoading] = useState(true)
  const [pending, setPending] = useState(false)
  const [loadError, setLoadError] = useState("")
  const [username, setUsername] = useState("")
  const [displayName, setDisplayName] = useState("")
  const [role, setRole] = useState<AdminRole>("viewer")
  const [password, setPassword] = useState("")
  const [secret, setSecret] = useState<{ title: string; value: string } | null>(
    null
  )

  const refresh = useCallback(
    async (initial = false) => {
      setLoadError("")
      try {
        const result = await load()
        setUsers(result.data)
      } catch (reason) {
        const message =
          reason instanceof Error ? reason.message : "無法載入使用者。"
        if (initial) setLoadError(message)
        else toast.error("更新使用者失敗", { description: message })
      } finally {
        setLoading(false)
        setPending(false)
      }
    },
    [load]
  )

  useEffect(() => {
    void refresh(true)
  }, [refresh])

  async function submit(event: FormEvent) {
    event.preventDefault()
    setPending(true)
    try {
      const body = {
        username,
        display_name: displayName,
        role,
        must_change_password: true,
        ...(password ? { password } : {}),
      }
      const result = await create({
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
      await refresh()
    } catch (reason) {
      toast.error("建立使用者失敗", {
        description:
          reason instanceof Error ? reason.message : "建立使用者失敗。",
      })
      setPending(false)
    }
  }

  async function changeRole(user: AdminUser, nextRole: AdminRole) {
    setPending(true)
    try {
      await update({ data: { userId: user.user_id, role: nextRole } })
      toast.success("角色已更新", {
        description: `${user.username} 已改為 ${nextRole}。`,
      })
      await refresh()
    } catch (reason) {
      toast.error("角色更新失敗", {
        description:
          reason instanceof Error ? reason.message : "角色更新失敗。",
      })
      setPending(false)
    }
  }

  async function toggleActive(user: AdminUser) {
    const action = user.is_active ? "停用" : "啟用"
    if (!window.confirm(`確定${action}「${user.username}」？`)) return
    setPending(true)
    try {
      await update({
        data: { userId: user.user_id, is_active: !user.is_active },
      })
      toast.success(`使用者已${action}`, {
        description: `${user.username} 已${action}。`,
      })
      await refresh()
    } catch (reason) {
      toast.error(`${action}使用者失敗`, {
        description:
          reason instanceof Error ? reason.message : `${action}失敗。`,
      })
      setPending(false)
    }
  }

  async function reset(user: AdminUser) {
    if (!window.confirm(`確定重設「${user.username}」的密碼？`)) return
    setPending(true)
    try {
      const result = await resetPassword({ data: { userId: user.user_id } })
      if (result.temporary_password) {
        setSecret({
          title: `${result.data.username} 的臨時密碼`,
          value: result.temporary_password,
        })
      }
      toast.success("密碼已重設", {
        description: `${user.username} 下次登入時須更改密碼。`,
      })
      await refresh()
    } catch (reason) {
      toast.error("密碼重設失敗", {
        description:
          reason instanceof Error ? reason.message : "密碼重設失敗。",
      })
      setPending(false)
    }
  }

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

      {loadError && (
        <Alert variant="destructive">
          <AlertTitle>無法載入使用者</AlertTitle>
          <AlertDescription>{loadError}</AlertDescription>
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
              <Button className="w-full" type="submit" disabled={pending}>
                <Plus size={17} /> {pending ? "處理中…" : "建立"}
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
            disabled={pending}
            onClick={() => {
              setPending(true)
              void refresh()
            }}
          >
            <RefreshCw className={pending ? "animate-spin" : ""} size={16} />
            更新
          </Button>
        </CardHeader>
        <CardContent className="px-0">
          {loading ? (
            <div className="grid gap-2" role="status">
              <span className="sr-only">正在載入使用者</span>
              <Skeleton className="h-10" />
              <Skeleton className="h-10" />
              <Skeleton className="h-10" />
            </div>
          ) : users.length === 0 ? (
            <Alert role="status">
              <AlertDescription>目前沒有管理者使用者。</AlertDescription>
            </Alert>
          ) : (
            <Table scrollMode="page">
              <TableHeader>
                <TableRow>
                  <TableHead>使用者</TableHead>
                  <TableHead>角色</TableHead>
                  <TableHead>狀態</TableHead>
                  <TableHead>最後登入</TableHead>
                  <TableHead className="text-right">操作</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {users.map(user => (
                  <TableRow key={user.user_id}>
                    <TableCell>
                      <strong className="block">{user.display_name}</strong>
                      <span className="font-mono text-xs text-muted">
                        {user.username}
                      </span>
                    </TableCell>
                    <TableCell>
                      <select
                        aria-label={`${user.username} 的角色`}
                        className="h-9 rounded-lg border border-line bg-surface px-2 text-sm"
                        value={user.role}
                        disabled={pending}
                        onChange={event =>
                          void changeRole(user, event.target.value as AdminRole)
                        }
                      >
                        <option value="owner">Owner</option>
                        <option value="operator">Operator</option>
                        <option value="viewer">Viewer</option>
                      </select>
                    </TableCell>
                    <TableCell>
                      <Badge variant={user.is_active ? "default" : "secondary"}>
                        {user.is_active ? "active" : "inactive"}
                      </Badge>
                      {user.must_change_password && (
                        <Badge className="ml-1" variant="warning">
                          須改密碼
                        </Badge>
                      )}
                    </TableCell>
                    <TableCell>{formatDate(user.last_login_at)}</TableCell>
                    <TableCell>
                      <div className="flex justify-end gap-1">
                        <Button
                          type="button"
                          size="icon"
                          variant="ghost"
                          aria-label={`重設 ${user.username} 的密碼`}
                          disabled={pending}
                          onClick={() => void reset(user)}
                        >
                          <KeyRound size={16} />
                        </Button>
                        <Button
                          type="button"
                          size="icon"
                          variant="ghost"
                          aria-label={`${user.is_active ? "停用" : "啟用"} ${user.username}`}
                          disabled={pending}
                          onClick={() => void toggleActive(user)}
                        >
                          {user.is_active ? (
                            <UserX size={16} />
                          ) : (
                            <UserCheck size={16} />
                          )}
                        </Button>
                      </div>
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
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
