import { Link } from "@tanstack/react-router"
import {
  Activity,
  ArrowRight,
  BookOpenCheck,
  DatabaseZap,
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

export default function LandingPage() {
  return (
    <main>
      <section className="mx-auto grid min-h-[calc(100vh-10rem)] w-full max-w-screen-2xl items-center gap-10 px-4 py-14 sm:px-6 sm:py-20 lg:grid-cols-[minmax(0,1.05fr)_minmax(30rem,0.95fr)] lg:gap-16 lg:px-8">
        <div className="max-w-3xl">
          <Badge variant="outline" className="mb-6">
            <Activity aria-hidden="true" />
            Financial data infrastructure
          </Badge>
          <h1 className="text-5xl leading-[0.95] font-extrabold tracking-tighter text-balance sm:text-6xl lg:text-7xl">
            從資料探索，到營運監控的統一入口
          </h1>
          <p className="mt-7 max-w-2xl text-base leading-8 text-muted sm:text-lg">
            查詢 FinDB
            收錄的金融商品、取得整合指引，或進入受保護的資料導入營運台。
          </p>
          <div className="mt-8 flex flex-col gap-3 sm:flex-row">
            <Button asChild size="lg">
              <Link to="/lookup" search={DEFAULT_SEARCH} preload="intent">
                搜尋金融商品
                <ArrowRight aria-hidden="true" />
              </Link>
            </Button>
            <Button asChild size="lg" variant="outline">
              <Link to="/skill" preload="intent">
                查看 Skill 指引
              </Link>
            </Button>
          </div>
        </div>

        <div className="grid gap-4">
          <Card className="overflow-hidden border-accent/30 bg-accent-soft/50">
            <CardHeader>
              <span className="mb-2 inline-flex size-11 items-center justify-center rounded-xl bg-accent text-white">
                <Search aria-hidden="true" />
              </span>
              <CardTitle>Instrument Lookup</CardTitle>
              <CardDescription>
                依市場、代號與名稱快速探索金融商品與識別資訊。
              </CardDescription>
            </CardHeader>
            <CardContent>
              <Button asChild variant="link" className="h-auto px-0">
                <Link to="/lookup" search={DEFAULT_SEARCH} preload="intent">
                  開啟公開查詢
                  <ArrowRight aria-hidden="true" />
                </Link>
              </Button>
            </CardContent>
          </Card>

          <div className="grid gap-4 sm:grid-cols-2">
            <Card>
              <CardHeader>
                <BookOpenCheck
                  className="mb-2 text-accent"
                  aria-hidden="true"
                />
                <CardTitle className="text-lg">FinDB Skill</CardTitle>
                <CardDescription>
                  取得安裝步驟與可直接使用的整合說明。
                </CardDescription>
              </CardHeader>
              <CardContent>
                <Link
                  to="/skill"
                  preload="intent"
                  className="inline-flex items-center gap-1.5 text-sm font-bold text-accent underline-offset-4 hover:underline"
                >
                  查看指引
                  <ArrowRight aria-hidden="true" className="size-4" />
                </Link>
              </CardContent>
            </Card>

            <Card>
              <CardHeader>
                <ShieldCheck className="mb-2 text-accent" aria-hidden="true" />
                <CardTitle className="text-lg">Operations</CardTitle>
                <CardDescription>
                  檢查導入穩定度、資料品質與稽核紀錄。
                </CardDescription>
              </CardHeader>
              <CardContent>
                <Link
                  to="/operations"
                  preload="intent"
                  className="inline-flex items-center gap-1.5 text-sm font-bold text-accent underline-offset-4 hover:underline"
                >
                  登入營運台
                  <ArrowRight aria-hidden="true" className="size-4" />
                </Link>
              </CardContent>
            </Card>
          </div>
        </div>
      </section>

      <section className="border-y border-line bg-surface/60">
        <div className="mx-auto grid w-full max-w-screen-2xl gap-8 px-4 py-10 sm:px-6 md:grid-cols-3 lg:px-8">
          <div className="flex gap-4">
            <DatabaseZap
              className="mt-1 shrink-0 text-accent"
              aria-hidden="true"
            />
            <div>
              <h2 className="font-bold">Fetch</h2>
              <p className="mt-1 text-sm leading-6 text-muted">
                接收並保留來源資料，建立可追蹤的導入紀錄。
              </p>
            </div>
          </div>
          <div className="flex gap-4">
            <ShieldCheck
              className="mt-1 shrink-0 text-accent"
              aria-hidden="true"
            />
            <div>
              <h2 className="font-bold">Normalize</h2>
              <p className="mt-1 text-sm leading-6 text-muted">
                統一欄位與識別規則，檢查資料完整性及正確性。
              </p>
            </div>
          </div>
          <div className="flex gap-4">
            <Search className="mt-1 shrink-0 text-accent" aria-hidden="true" />
            <div>
              <h2 className="font-bold">Serve</h2>
              <p className="mt-1 text-sm leading-6 text-muted">
                提供唯讀資料服務與易於使用的公開查詢工具。
              </p>
            </div>
          </div>
        </div>
      </section>
    </main>
  )
}
