import { createFileRoute, redirect } from "@tanstack/react-router"
import { useServerFn } from "@tanstack/react-start"
import { LockKeyhole, LogIn, TriangleAlert } from "lucide-react"
import { type FormEvent, useState } from "react"

import { getSession, login } from "../lib/auth.functions"

export const Route = createFileRoute("/login")({
  beforeLoad: async () => {
    const session = await getSession()
    if (session.authenticated) throw redirect({ to: "/" })
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
      await navigate({ to: "/" })
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "登入失敗。")
    } finally {
      setPending(false)
    }
  }

  return (
    <main className="login-shell">
      <section className="login-card">
        <span className="login-mark">
          <LockKeyhole size={25} />
        </span>
        <p className="eyebrow">Restricted operations access</p>
        <h1>登入 FinDB Dashboard</h1>
        <p className="login-copy">
          使用部署環境設定的操作帳號登入。本服務不提供註冊功能。
        </p>
        <form className="login-form" onSubmit={submit}>
          <label>
            帳號
            <input
              autoComplete="username"
              value={username}
              onChange={event => setUsername(event.target.value)}
              required
            />
          </label>
          <label>
            密碼
            <input
              type="password"
              autoComplete="current-password"
              value={password}
              onChange={event => setPassword(event.target.value)}
              required
            />
          </label>
          {error && (
            <div className="global-error" role="alert">
              <TriangleAlert size={17} />
              {error}
            </div>
          )}
          <button type="submit" disabled={pending}>
            <LogIn size={17} />
            {pending ? "驗證中…" : "登入"}
          </button>
        </form>
      </section>
    </main>
  )
}
