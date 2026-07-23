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
    <main className="mx-auto grid min-h-[calc(100vh-10rem)] w-full max-w-lg place-items-center px-4 py-14">
      <section className="w-full rounded-2xl border border-line bg-surface p-6 shadow-panel sm:p-10">
        <span className="mb-6 inline-flex size-12 items-center justify-center rounded-2xl bg-accent-soft text-accent">
          <LockKeyhole size={25} />
        </span>
        <p className="mb-1 font-mono text-xs font-medium tracking-widest text-accent uppercase">
          Restricted operations access
        </p>
        <h1 className="my-2.5 text-3xl tracking-tight sm:text-4xl">
          登入 FinDB Dashboard
        </h1>
        <p className="mt-0 mb-7 leading-relaxed text-muted">
          使用部署環境設定的操作帳號登入。本服務不提供註冊功能。
        </p>
        <form className="grid gap-4" onSubmit={submit}>
          <label className="grid gap-2 text-xs font-bold text-muted">
            帳號
            <input
              className="w-full min-w-0 rounded-lg border border-line bg-surface px-3 py-2.5 text-ink outline-none focus:border-accent focus:ring-3 focus:ring-accent/15"
              autoComplete="username"
              value={username}
              onChange={event => setUsername(event.target.value)}
              required
            />
          </label>
          <label className="grid gap-2 text-xs font-bold text-muted">
            密碼
            <input
              className="w-full min-w-0 rounded-lg border border-line bg-surface px-3 py-2.5 text-ink outline-none focus:border-accent focus:ring-3 focus:ring-accent/15"
              type="password"
              autoComplete="current-password"
              value={password}
              onChange={event => setPassword(event.target.value)}
              required
            />
          </label>
          {error && (
            <div
              className="flex items-center gap-2.5 rounded-xl border border-danger/30 bg-danger-soft px-3.5 py-3 text-xs text-danger"
              role="alert"
            >
              <TriangleAlert size={17} />
              {error}
            </div>
          )}
          <button
            className="inline-flex cursor-pointer items-center justify-center gap-2 rounded-lg bg-accent px-4 py-2.5 text-sm font-extrabold whitespace-nowrap text-white disabled:cursor-not-allowed disabled:opacity-55"
            type="submit"
            disabled={pending}
          >
            <LogIn size={17} />
            {pending ? "驗證中…" : "登入"}
          </button>
        </form>
      </section>
    </main>
  )
}
