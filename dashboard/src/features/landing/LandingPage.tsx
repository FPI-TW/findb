import { Link } from "@tanstack/react-router"
import {
  Activity,
  Archive,
  ArrowRight,
  BookOpenCheck,
  ChartNoAxesCombined,
  Clock3,
  Database,
  Gauge,
  LockKeyhole,
  Search,
  ShieldCheck,
} from "lucide-react"

import { Badge } from "../../components/ui/badge"
import { Button } from "../../components/ui/button"
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "../../components/ui/card"
import { DEFAULT_SEARCH } from "../lookup/config"

const MACRO_SEARCH = {
  ...DEFAULT_SEARCH,
  ds: "macro" as const,
}

export default function LandingPage() {
  return (
    <main className="mx-auto w-full max-w-screen-2xl px-3 py-6 sm:px-5 sm:py-9 sm:pb-16">
      <header className="mb-7 border-b border-line px-1 pb-6">
        <div className="flex flex-col justify-between gap-4 md:flex-row md:items-end">
          <div>
            <p className="mb-1 flex items-center gap-2 font-mono text-xs font-medium tracking-widest text-accent uppercase">
              <Activity className="size-4" aria-hidden="true" />
              FinDB Workspace
            </p>
            <h1 className="text-3xl font-extrabold tracking-tight sm:text-4xl">
              資料工作台
            </h1>
            <p className="mt-2 max-w-2xl text-sm leading-6 text-muted">
              查詢金融資料、監控導入狀態，或開啟整合文件。
            </p>
          </div>
          <div className="flex flex-wrap items-center gap-2 text-xs text-muted">
            <Badge variant="outline">
              <Search aria-hidden="true" />
              Lookup 公開
            </Badge>
            <Badge variant="outline">
              <LockKeyhole aria-hidden="true" />
              Operations 需登入
            </Badge>
          </div>
        </div>
      </header>

      <div className="grid gap-6 xl:grid-cols-[minmax(0,1fr)_19rem]">
        <section aria-labelledby="primary-workspaces-title">
          <div className="mb-3 flex items-center justify-between gap-3 px-1">
            <div>
              <p className="font-mono text-xs font-medium tracking-widest text-muted uppercase">
                Primary workspaces
              </p>
              <h2
                className="mt-1 text-xl font-bold tracking-tight"
                id="primary-workspaces-title"
              >
                主要工作區
              </h2>
            </div>
          </div>

          <div className="grid gap-5 lg:grid-cols-2">
            <Card className="gap-0 overflow-hidden border-accent/30">
              <CardHeader className="border-b border-line bg-accent-soft/55 py-5">
                <div className="mb-3 flex items-center justify-between gap-3">
                  <span className="inline-flex size-10 items-center justify-center rounded-xl bg-accent text-white">
                    <Search aria-hidden="true" />
                  </span>
                  <Badge variant="outline">公開工具</Badge>
                </div>
                <CardTitle asChild className="text-2xl tracking-tight">
                  <h3>Lookup</h3>
                </CardTitle>
                <CardDescription className="leading-6">
                  搜尋金融商品與宏觀序列，檢視識別資料、價格及相關紀錄。
                </CardDescription>
              </CardHeader>
              <CardContent className="grid gap-4 py-5">
                <div className="grid gap-2 sm:grid-cols-2">
                  <Link
                    to="/lookup"
                    search={DEFAULT_SEARCH}
                    preload="intent"
                    className="group rounded-xl border border-line bg-surface-soft p-3 outline-none transition-colors hover:border-accent/40 hover:bg-accent-soft/55 focus-visible:ring-3 focus-visible:ring-accent/20"
                  >
                    <Database
                      className="mb-3 size-5 text-accent"
                      aria-hidden="true"
                    />
                    <strong className="block text-sm">金融商品</strong>
                    <span className="mt-1 block text-xs leading-5 text-muted">
                      Symbol、名稱、市場與資產類別
                    </span>
                    <ArrowRight
                      className="mt-3 size-4 text-accent transition-transform group-hover:translate-x-0.5"
                      aria-hidden="true"
                    />
                  </Link>
                  <Link
                    to="/lookup"
                    search={MACRO_SEARCH}
                    preload="intent"
                    className="group rounded-xl border border-line bg-surface-soft p-3 outline-none transition-colors hover:border-accent/40 hover:bg-accent-soft/55 focus-visible:ring-3 focus-visible:ring-accent/20"
                  >
                    <ChartNoAxesCombined
                      className="mb-3 size-5 text-accent"
                      aria-hidden="true"
                    />
                    <strong className="block text-sm">宏觀序列</strong>
                    <span className="mt-1 block text-xs leading-5 text-muted">
                      頻率、單位、來源與觀察值
                    </span>
                    <ArrowRight
                      className="mt-3 size-4 text-accent transition-transform group-hover:translate-x-0.5"
                      aria-hidden="true"
                    />
                  </Link>
                </div>
                <Button asChild>
                  <Link to="/lookup" search={DEFAULT_SEARCH} preload="intent">
                    開啟 Lookup
                    <ArrowRight aria-hidden="true" />
                  </Link>
                </Button>
              </CardContent>
            </Card>

            <Card className="gap-0 overflow-hidden">
              <CardHeader className="border-b border-line bg-surface-soft py-5">
                <div className="mb-3 flex items-center justify-between gap-3">
                  <span className="inline-flex size-10 items-center justify-center rounded-xl bg-ink text-page">
                    <Gauge aria-hidden="true" />
                  </span>
                  <Badge variant="outline">
                    <LockKeyhole aria-hidden="true" />
                    需登入
                  </Badge>
                </div>
                <CardTitle asChild className="text-2xl tracking-tight">
                  <h3>Operations</h3>
                </CardTitle>
                <CardDescription className="leading-6">
                  監控導入健康度、資料完整性、DQ 問題及稽核紀錄。
                </CardDescription>
              </CardHeader>
              <CardContent className="grid gap-4 py-5">
                <nav
                  className="grid grid-cols-2 gap-2"
                  aria-label="Operations 快捷入口"
                >
                  <WorkspaceLink
                    to="/operations"
                    icon={<Gauge aria-hidden="true" />}
                    label="導入概況"
                  />
                  <WorkspaceLink
                    to="/operations/deliveries"
                    icon={<Clock3 aria-hidden="true" />}
                    label="缺漏交付"
                  />
                  <WorkspaceLink
                    to="/operations/quality"
                    icon={<ShieldCheck aria-hidden="true" />}
                    label="資料品質"
                  />
                  <WorkspaceLink
                    to="/operations/corrections"
                    icon={<Archive aria-hidden="true" />}
                    label="修正稽核"
                  />
                </nav>
                <Button asChild variant="outline">
                  <Link to="/operations" preload="intent">
                    進入 Operations
                    <ArrowRight aria-hidden="true" />
                  </Link>
                </Button>
              </CardContent>
            </Card>
          </div>
        </section>

        <aside className="grid content-start gap-4" aria-label="輔助資源">
          <div className="px-1">
            <p className="font-mono text-xs font-medium tracking-widest text-muted uppercase">
              Supporting resources
            </p>
            <h2 className="mt-1 text-xl font-bold tracking-tight">輔助資源</h2>
          </div>

          <Card className="gap-0 shadow-none">
            <CardHeader className="py-5">
              <span className="mb-2 inline-flex size-9 items-center justify-center rounded-xl bg-accent-soft text-accent">
                <BookOpenCheck aria-hidden="true" />
              </span>
              <CardTitle asChild className="text-lg">
                <h3>FinDB Skill</h3>
              </CardTitle>
              <CardDescription className="leading-6">
                安裝 Codex Skill，取得資料查詢方式與 API 使用指引。
              </CardDescription>
            </CardHeader>
            <CardContent className="pb-5">
              <Button asChild variant="secondary" className="w-full">
                <Link to="/skill" preload="intent">
                  查看 Skill
                  <ArrowRight aria-hidden="true" />
                </Link>
              </Button>
            </CardContent>
          </Card>

          <div className="rounded-xl border border-line bg-surface-soft p-4">
            <p className="text-xs font-bold tracking-wide text-muted uppercase">
              資料流程
            </p>
            <div className="mt-3 flex items-center gap-2 text-xs font-bold text-ink">
              <span>Fetch</span>
              <ArrowRight className="size-3 text-muted" aria-hidden="true" />
              <span>Normalize</span>
              <ArrowRight className="size-3 text-muted" aria-hidden="true" />
              <span>Serve</span>
            </div>
          </div>
        </aside>
      </div>
    </main>
  )
}

function WorkspaceLink({
  to,
  icon,
  label,
}: {
  to:
    | "/operations"
    | "/operations/deliveries"
    | "/operations/quality"
    | "/operations/corrections"
  icon: React.ReactNode
  label: string
}) {
  return (
    <Link
      to={to}
      preload="intent"
      className="flex min-h-11 items-center gap-2 rounded-lg border border-line bg-surface-soft px-3 text-sm font-bold text-ink outline-none transition-colors hover:border-accent/40 hover:bg-accent-soft hover:text-accent focus-visible:ring-3 focus-visible:ring-accent/20"
    >
      <span className="text-accent [&>svg]:size-4">{icon}</span>
      {label}
    </Link>
  )
}
