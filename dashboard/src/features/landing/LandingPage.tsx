import { Link } from "@tanstack/react-router"
import {
  Activity,
  ArrowRight,
  BookOpenCheck,
  ChartNoAxesCombined,
  CircleCheckBig,
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
              金融資料工作台
            </h1>
            <p className="mt-2 max-w-2xl text-sm leading-6 text-muted">
              從 canonical
              資料查詢到導入品質監控，提供一致、唯讀且可追溯的操作入口。
            </p>
          </div>
          <div className="flex flex-wrap items-center gap-2 text-xs text-muted">
            <Badge variant="outline">
              <Search aria-hidden="true" />
              Canonical 唯讀
            </Badge>
            <Badge variant="outline">
              <CircleCheckBig aria-hidden="true" />
              DQ 與稽核可追溯
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
            <Card className="h-full gap-0 overflow-hidden rounded-xl border-t-2 border-t-accent shadow-sm">
              <CardHeader className="min-h-48 border-b border-line bg-surface py-5">
                <div className="mb-3 flex items-center justify-between gap-3">
                  <span className="inline-flex size-10 items-center justify-center rounded-lg bg-accent text-white">
                    <Search aria-hidden="true" />
                  </span>
                  <Badge variant="outline">Serve / Read only</Badge>
                </div>
                <CardTitle asChild className="text-2xl tracking-tight">
                  <h3>Lookup</h3>
                </CardTitle>
                <CardDescription className="leading-6">
                  僅從 canonical layer
                  查詢金融商品與宏觀序列，維持一致的識別與欄位定義。
                </CardDescription>
              </CardHeader>
              <CardContent className="grid flex-1 grid-rows-[1fr_auto] gap-4 py-5">
                <div className="grid gap-2 sm:grid-cols-2">
                  <Link
                    to="/lookup"
                    search={DEFAULT_SEARCH}
                    preload="intent"
                    className="group rounded-lg border border-line bg-surface-soft p-3 outline-none transition-colors hover:border-accent/40 hover:bg-accent-soft/55 focus-visible:ring-3 focus-visible:ring-accent/20"
                  >
                    <Database
                      className="mb-3 size-5 text-accent"
                      aria-hidden="true"
                    />
                    <strong className="block text-sm">金融商品</strong>
                    <span className="mt-1 block text-xs leading-5 text-muted">
                      Canonical identifier 與市場屬性
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
                    className="group rounded-lg border border-line bg-surface-soft p-3 outline-none transition-colors hover:border-accent/40 hover:bg-accent-soft/55 focus-visible:ring-3 focus-visible:ring-accent/20"
                  >
                    <ChartNoAxesCombined
                      className="mb-3 size-5 text-accent"
                      aria-hidden="true"
                    />
                    <strong className="block text-sm">宏觀序列</strong>
                    <span className="mt-1 block text-xs leading-5 text-muted">
                      統一頻率、單位與來源定義
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

            <Card className="h-full gap-0 overflow-hidden rounded-xl border-t-2 border-t-ink shadow-sm">
              <CardHeader className="min-h-48 border-b border-line bg-surface py-5">
                <div className="mb-3 flex items-center justify-between gap-3">
                  <span className="inline-flex size-10 items-center justify-center rounded-lg bg-ink text-page">
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
                  檢視導入健康度、資料完整性與 DQ 結果，追蹤原始資料及後續修正。
                </CardDescription>
              </CardHeader>
              <CardContent className="grid flex-1 grid-rows-[1fr_auto] gap-4 py-5">
                <nav
                  className="grid gap-2 sm:grid-cols-2"
                  aria-label="Operations 快捷入口"
                >
                  <WorkspaceLink
                    to="/operations"
                    icon={<Gauge aria-hidden="true" />}
                    label="營運監控"
                    description="佇列、Worker 與缺漏交付"
                  />
                  <WorkspaceLink
                    to="/operations/quality"
                    icon={<ShieldCheck aria-hidden="true" />}
                    label="品質與稽核"
                    description="DQ、修正紀錄與原始資料"
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

          <Card className="gap-0 rounded-xl shadow-none">
            <CardHeader className="py-5">
              <span className="mb-2 inline-flex size-9 items-center justify-center rounded-lg bg-surface-soft text-accent">
                <BookOpenCheck aria-hidden="true" />
              </span>
              <CardTitle asChild className="text-base">
                <h3>FinDB Skill</h3>
              </CardTitle>
              <CardDescription className="leading-6">
                查閱資料合約、查詢方式與 API 使用指引。
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
        </aside>
      </div>
    </main>
  )
}

function WorkspaceLink({
  to,
  icon,
  label,
  description,
}: {
  to: "/operations" | "/operations/quality"
  icon: React.ReactNode
  label: string
  description: string
}) {
  return (
    <Link
      to={to}
      preload="intent"
      className="group rounded-lg border border-line bg-surface-soft p-3 outline-none transition-colors hover:border-accent/40 hover:bg-accent-soft/55 focus-visible:ring-3 focus-visible:ring-accent/20"
    >
      <span className="mb-3 block text-accent [&>svg]:size-5">{icon}</span>
      <strong className="block text-sm">{label}</strong>
      <span className="mt-1 block text-xs leading-5 text-muted">
        {description}
      </span>
      <ArrowRight
        className="mt-3 size-4 text-accent transition-transform group-hover:translate-x-0.5"
        aria-hidden="true"
      />
    </Link>
  )
}
