import { createFileRoute } from "@tanstack/react-router"
import { useServerFn } from "@tanstack/react-start"
import { KeyRound, TriangleAlert } from "lucide-react"
import { type FormEvent, useState } from "react"

import { Alert, AlertDescription, AlertTitle } from "../../components/ui/alert"
import { Button } from "../../components/ui/button"
import {
  Card,
  CardContent,
  CardHeader,
  CardTitle,
} from "../../components/ui/card"
import { Input } from "../../components/ui/input"
import { Label } from "../../components/ui/label"
import { changePassword } from "../../lib/auth.functions"

export const Route = createFileRoute("/_authenticated/change-password")({
  component: ChangePasswordPage,
})

function ChangePasswordPage() {
  const change = useServerFn(changePassword)
  const navigate = Route.useNavigate()
  const [currentPassword, setCurrentPassword] = useState("")
  const [newPassword, setNewPassword] = useState("")
  const [confirmPassword, setConfirmPassword] = useState("")
  const [error, setError] = useState("")
  const [pending, setPending] = useState(false)

  async function submit(event: FormEvent) {
    event.preventDefault()
    setPending(true)
    setError("")
    try {
      await change({
        data: { currentPassword, newPassword, confirmPassword },
      })
      await navigate({ to: "/login" })
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "密碼更新失敗。")
      setPending(false)
    }
  }

  return (
    <main className="mx-auto grid min-h-[calc(100vh-10rem)] w-full max-w-lg place-items-center px-4 py-14">
      <Card className="w-full py-8">
        <CardHeader className="gap-3 px-6 sm:px-10">
          <span className="inline-flex size-12 items-center justify-center rounded-2xl bg-accent-soft text-accent">
            <KeyRound size={24} />
          </span>
          <CardTitle asChild className="text-3xl tracking-tight">
            <h1>設定新密碼</h1>
          </CardTitle>
          <p className="text-sm text-muted">
            臨時密碼首次登入後必須更換，完成後請使用新密碼重新登入。
          </p>
        </CardHeader>
        <CardContent className="px-6 sm:px-10">
          <form className="grid gap-4" onSubmit={submit}>
            <div className="grid gap-2">
              <Label htmlFor="current-password">目前密碼</Label>
              <Input
                id="current-password"
                type="password"
                autoComplete="current-password"
                value={currentPassword}
                onChange={event => setCurrentPassword(event.target.value)}
                required
              />
            </div>
            <div className="grid gap-2">
              <Label htmlFor="new-password">新密碼（至少 12 字元）</Label>
              <Input
                id="new-password"
                type="password"
                autoComplete="new-password"
                minLength={12}
                value={newPassword}
                onChange={event => setNewPassword(event.target.value)}
                required
              />
            </div>
            <div className="grid gap-2">
              <Label htmlFor="confirm-password">確認新密碼</Label>
              <Input
                id="confirm-password"
                type="password"
                autoComplete="new-password"
                value={confirmPassword}
                onChange={event => setConfirmPassword(event.target.value)}
                required
              />
            </div>
            {error && (
              <Alert variant="destructive">
                <TriangleAlert size={17} />
                <AlertTitle>更新失敗</AlertTitle>
                <AlertDescription>{error}</AlertDescription>
              </Alert>
            )}
            <Button type="submit" disabled={pending}>
              <KeyRound size={17} />
              {pending ? "更新中…" : "更新密碼"}
            </Button>
          </form>
        </CardContent>
      </Card>
    </main>
  )
}
