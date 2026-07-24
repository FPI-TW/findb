import {
  ArrowLeft,
  BookOpenCheck,
  Download,
  PackageOpen,
  Sparkles,
} from "lucide-react"

import { Alert, AlertDescription } from "../../components/ui/alert"
import { Badge } from "../../components/ui/badge"
import { Button } from "../../components/ui/button"
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "../../components/ui/card"
import InstallTabs from "./InstallTabs"
import { skillArchive, skillContents, skillFiles } from "./skill-data"

function StepHeading({
  number,
  children,
}: {
  number: number
  children: React.ReactNode
}) {
  return (
    <div className="flex items-center gap-3">
      <span className="inline-flex size-8 shrink-0 items-center justify-center rounded-full bg-accent font-mono text-sm font-bold text-white">
        {number}
      </span>
      <CardTitle asChild className="text-xl">
        <h2>{children}</h2>
      </CardTitle>
    </div>
  )
}

export default function SkillPage() {
  return (
    <main className="mx-auto w-full max-w-5xl px-4 py-10 sm:px-6 sm:py-16 lg:px-8">
      <Button asChild variant="link" className="mb-6 h-auto px-0">
        <a href="./">
          <ArrowLeft aria-hidden="true" />
          回公開入口
        </a>
      </Button>

      <header className="mb-10 max-w-3xl">
        <Badge variant="outline" className="mb-5">
          <Sparkles aria-hidden="true" />
          AI Assistant Skill
        </Badge>
        <h1 className="text-4xl leading-tight font-extrabold tracking-tight text-balance sm:text-5xl">
          FinDB <span className="text-accent">·</span> Skill 安裝教學
        </h1>
        <p className="mt-5 text-base leading-8 text-muted sm:text-lg">
          把 FinDB API 用法打包成 SKILL.md，讓 Claude Code、Codex、Cursor 等 AI
          開發工具在你開發 FinDB 客戶端、儀表板、ETL 流程時自動理解認證、Source
          / Serve / Admin 端點與 payload 格式。
        </p>
      </header>

      <div className="grid gap-6">
        <Card>
          <CardHeader>
            <StepHeading number={1}>下載 .skill 檔</StepHeading>
            <CardDescription>
              取得最新的 FinDB API 指引封裝，下載後可直接解壓安裝。
            </CardDescription>
          </CardHeader>
          <CardContent className="grid gap-6 lg:grid-cols-2">
            <div className="flex flex-col items-start justify-center gap-5">
              <Button asChild size="lg">
                <a href="/static/findb-api.skill" download>
                  <Download aria-hidden="true" />
                  下載 findb-api.skill
                </a>
              </Button>
              <dl className="flex flex-wrap gap-x-6 gap-y-3 text-sm">
                <div>
                  <dt className="text-xs font-bold text-muted">檔名</dt>
                  <dd className="mt-1 font-mono">{skillArchive.filename}</dd>
                </div>
                <div>
                  <dt className="text-xs font-bold text-muted">格式</dt>
                  <dd className="mt-1 font-mono">{skillArchive.format}</dd>
                </div>
                <div>
                  <dt className="text-xs font-bold text-muted">大小</dt>
                  <dd className="mt-1 font-mono">{skillArchive.size}</dd>
                </div>
              </dl>
              <p className="text-sm leading-7 text-muted">
                <code className="rounded bg-surface-soft px-1.5 py-0.5 font-mono">
                  .skill
                </code>{" "}
                是 zip 壓縮包，包含 SKILL.md 主檔、三份 references
                詳細規格與一份範例 payload。
              </p>
            </div>

            <div
              className="rounded-xl border border-line bg-surface-soft p-4 font-mono text-xs"
              aria-label="findb-api.skill 內容"
            >
              <div className="mb-3 flex items-center justify-between gap-3 border-b border-dashed border-line pb-3">
                <span className="inline-flex items-center gap-2 font-bold">
                  <PackageOpen
                    className="size-4 text-accent"
                    aria-hidden="true"
                  />
                  findb-api/
                </span>
                <span className="text-muted">
                  {skillArchive.fileCount} files
                </span>
              </div>
              <ul className="grid gap-2">
                {skillFiles.map(file => (
                  <li
                    key={file.name}
                    className="flex min-w-0 items-baseline gap-2"
                  >
                    <span
                      className={
                        file.directory
                          ? "min-w-0 truncate font-bold text-accent"
                          : "min-w-0 truncate text-ink"
                      }
                    >
                      {file.name}
                    </span>
                    {file.size && (
                      <>
                        <span
                          className="min-w-3 flex-1 border-b border-dotted border-muted/50"
                          aria-hidden="true"
                        />
                        <span className="shrink-0 text-muted">{file.size}</span>
                      </>
                    )}
                  </li>
                ))}
              </ul>
            </div>
          </CardContent>
        </Card>

        <Card>
          <CardHeader>
            <StepHeading number={2}>安裝到 AI 開發工具</StepHeading>
            <CardDescription>
              選擇使用的工具，依系統提示複製對應指令。
            </CardDescription>
          </CardHeader>
          <CardContent>
            <InstallTabs />
          </CardContent>
        </Card>

        <Card>
          <CardHeader>
            <StepHeading number={3}>Skill 涵蓋的內容</StepHeading>
            <CardDescription>
              主指令、完整規格與可直接送出的範例資料都包含在封裝中。
            </CardDescription>
          </CardHeader>
          <CardContent>
            <div className="grid gap-4 md:grid-cols-2">
              {skillContents.map(content => (
                <article
                  key={content.name}
                  className={
                    content.primary
                      ? "rounded-xl border border-accent/30 bg-accent-soft/50 p-5 md:col-span-2"
                      : "rounded-xl border border-line bg-surface-soft p-5"
                  }
                >
                  <div className="flex flex-wrap items-center gap-2">
                    <Badge variant={content.primary ? "default" : "outline"}>
                      {content.role}
                    </Badge>
                    <h3 className="font-mono font-bold text-ink">
                      {content.name}
                    </h3>
                  </div>
                  <p className="mt-3 text-sm leading-7 text-muted">
                    {content.description}
                  </p>
                  <div className="mt-4 flex flex-wrap gap-2">
                    {content.tags.map(tag => (
                      <Badge key={tag} variant="secondary">
                        {tag}
                      </Badge>
                    ))}
                  </div>
                </article>
              ))}
            </div>

            <Alert className="mt-5" variant="subtle" role="note">
              <BookOpenCheck aria-hidden="true" />
              <AlertDescription>
                Skill 內容會隨 FinDB API 演進同步更新。看到 AI
                給出過時欄位時，回到本頁重新下載最新版即可。
              </AlertDescription>
            </Alert>
          </CardContent>
        </Card>
      </div>

      <p className="mt-8 text-center text-xs text-muted">
        FinDB · AI Skill v1 · 隨 API 變更同步更新
      </p>
    </main>
  )
}
