import { createFileRoute, redirect } from "@tanstack/react-router"
import { useServerFn } from "@tanstack/react-start"
import { LockKeyhole, LogIn, TriangleAlert } from "lucide-react"
import { type FormEvent, useState } from "react"

import { Alert, AlertDescription, AlertTitle } from "../components/ui/alert"
import { Button } from "../components/ui/button"
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "../components/ui/card"
import { Input } from "../components/ui/input"
import { Label } from "../components/ui/label"
import { getSession, login } from "../lib/auth.functions"

export const Route = createFileRoute("/login")({
  beforeLoad: async () => {
    const session = await getSession()
    if (session.authenticated) throw redirect({ to: "/operations" })
  },
  component: LoginPage,
})

function LoginPage() {
  const loginFn = useServerFn(login)
  const navigate = Route.useNavigate()
  const [username, setUsername] = useState("")
  const [password, setPassword] = useState("")
  const [error, setError] = useState("")
  const [pending, setPending] = useState(false)

  async function submit(event: FormEvent) {
    event.preventDefault()
    setPending(true)
    setError("")
    try {
      await loginFn({ data: { username, password } })
      await navigate({ to: "/operations" })
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "登入失敗。")
    } finally {
      setPending(false)
    }
  }

  return (
    <main className="mx-auto grid min-h-[calc(100vh-10rem)] w-full max-w-lg place-items-center px-4 py-14">
      <Card className="w-full py-8 sm:py-10">
        <CardHeader className="gap-4 px-6 sm:px-10">
          <span className="inline-flex size-12 items-center justify-center rounded-2xl bg-accent-soft text-accent">
            <LockKeyhole size={25} />
          </span>
          <div>
            <p className="mb-1 font-mono text-xs font-medium tracking-widest text-accent uppercase">
              Restricted operations access
            </p>
            <CardTitle asChild className="text-3xl tracking-tight sm:text-4xl">
              <h1>登入 FinDB Dashboard</h1>
            </CardTitle>
          </div>
          <CardDescription className="leading-relaxed">
            使用 FinDB 管理者帳號登入。本服務不提供公開註冊。
          </CardDescription>
        </CardHeader>
        <CardContent className="px-6 sm:px-10">
          <form className="grid gap-4" onSubmit={submit}>
            <div className="grid gap-2">
              <Label htmlFor="username">帳號</Label>
              <Input
                id="username"
                autoComplete="username"
                value={username}
                onChange={event => setUsername(event.target.value)}
                required
              />
            </div>
            <div className="grid gap-2">
              <Label htmlFor="password">密碼</Label>
              <Input
                id="password"
                type="password"
                autoComplete="current-password"
                value={password}
                onChange={event => setPassword(event.target.value)}
                required
              />
            </div>
            {error && (
              <Alert variant="destructive">
                <TriangleAlert size={17} />
                <AlertTitle>登入失敗</AlertTitle>
                <AlertDescription>{error}</AlertDescription>
              </Alert>
            )}
            <Button type="submit" disabled={pending}>
              <LogIn size={17} />
              {pending ? "驗證中…" : "登入"}
            </Button>
          </form>
        </CardContent>
      </Card>
    </main>
  )
}
