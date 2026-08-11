import { CheckCircle2 } from "lucide-react"
import {
  type KeyboardEvent,
  type ReactNode,
  useEffect,
  useId,
  useRef,
  useState,
} from "react"

import { Alert, AlertDescription, AlertTitle } from "../../components/ui/alert"
import { Badge } from "../../components/ui/badge"
import CopyButton from "./CopyButton"
import { detectOperatingSystem, type OperatingSystem } from "./clipboard"

type TabId = "claude" | "codex" | "cursor" | "manual"

const tabs: Array<{ id: TabId; label: string }> = [
  { id: "claude", label: "Claude Code" },
  { id: "codex", label: "Codex" },
  { id: "cursor", label: "Cursor / 其他" },
  { id: "manual", label: "手動 / 一次性" },
]

function CodeBlock({
  label,
  code,
  operatingSystem,
}: {
  label: string
  code: string
  operatingSystem?: OperatingSystem
}) {
  const [detectedSystem, setDetectedSystem] = useState<OperatingSystem | null>(
    null
  )

  useEffect(() => {
    setDetectedSystem(
      detectOperatingSystem(navigator.userAgent, navigator.platform)
    )
  }, [])

  return (
    <div className="overflow-hidden rounded-xl border border-line bg-surface-soft">
      <div className="flex min-h-11 items-center gap-2 border-b border-line px-3">
        <span className="text-xs font-bold text-muted">{label}</span>
        {operatingSystem && detectedSystem === operatingSystem && (
          <Badge variant="secondary">你的系統</Badge>
        )}
        <CopyButton text={code} label={`複製 ${label} 指令`} />
      </div>
      <pre className="m-0 overflow-x-auto p-4 font-mono text-xs leading-6 text-ink sm:text-sm">
        <code>{code}</code>
      </pre>
    </div>
  )
}

function Prompt({ children }: { children: string }) {
  return (
    <div className="mt-2 flex items-center overflow-hidden rounded-lg border border-accent/30 bg-surface">
      <code className="min-w-0 flex-1 overflow-x-auto px-3 py-2 font-mono text-xs whitespace-nowrap">
        {children}
      </code>
      <CopyButton text={children} label="複製提示" />
    </div>
  )
}

function ClaudeInstructions() {
  return (
    <div className="space-y-5">
      <p className="text-sm leading-7 text-muted">
        解壓到使用者層的 skills 目錄，新對話會自動載入。
      </p>
      <div className="grid gap-4">
        <CodeBlock
          label="macOS · Linux · WSL"
          operatingSystem="unix"
          code={`mkdir -p ~/.claude/skills
unzip findb-api.skill -d ~/.claude/skills/`}
        />
        <CodeBlock
          label="PowerShell"
          operatingSystem="windows"
          code={`New-Item -ItemType Directory -Force "$env:USERPROFILE\\.claude\\skills"
Expand-Archive findb-api.skill -DestinationPath "$env:USERPROFILE\\.claude\\skills"`}
        />
      </div>

      <Alert variant="success" role="note">
        <CheckCircle2 aria-hidden="true" />
        <AlertTitle>驗證安裝</AlertTitle>
        <AlertDescription>
          <ol className="mt-2 grid list-decimal gap-4 pl-5 text-ink">
            <li>
              開新 Claude Code session，輸入：
              <Prompt>從 staging FinDB 拉 AAPL 最近 30 天 EOD</Prompt>
              <span className="mt-2 block text-xs text-muted">
                預期：立刻產出帶{" "}
                <code className="rounded bg-surface px-1.5 py-0.5 font-mono">
                  X-API-Key
                </code>{" "}
                header 的請求程式碼。
              </span>
            </li>
            <li>
              或直接問：
              <Prompt>列出可用 Skills</Prompt>
              <span className="mt-2 block text-xs text-muted">
                預期：
                <code className="mx-1 rounded bg-surface px-1.5 py-0.5 font-mono">
                  findb-api
                </code>
                出現在清單中。
              </span>
            </li>
          </ol>
        </AlertDescription>
      </Alert>

      <div className="border-t border-line pt-5">
        <div className="mb-2 flex flex-wrap items-center gap-2">
          <h3 className="font-bold">團隊共用：專案層級安裝</h3>
          <Badge variant="outline">Optional</Badge>
        </div>
        <p className="mb-4 text-sm leading-7 text-muted">
          把整個{" "}
          <code className="rounded bg-surface-soft px-1.5 py-0.5 font-mono">
            findb-api/
          </code>{" "}
          目錄解到 client repo 內的{" "}
          <code className="rounded bg-surface-soft px-1.5 py-0.5 font-mono">
            .claude/skills/
          </code>{" "}
          並 commit 進版控，整個團隊 clone 後即生效，不必各自手動安裝。
        </p>
        <CodeBlock
          label="在 client repo 根目錄執行"
          code={`mkdir -p .claude/skills
unzip /path/to/findb-api.skill -d .claude/skills/
git add .claude/skills/findb-api
git commit -m "Add FinDB API skill"`}
        />
      </div>
    </div>
  )
}

function CodexInstructions() {
  return (
    <div className="space-y-5">
      <p className="text-sm leading-7 text-muted">
        Codex 不會自動依關鍵字載入 skill。推薦把內容塞進全域 instructions 或專案
        AGENTS.md。
      </p>
      <div>
        <h3 className="mb-3 font-bold">方法 A：全域 instructions（永久性）</h3>
        <CodeBlock
          label="shell"
          code={`unzip findb-api.skill -d /tmp/findb-skill
mkdir -p ~/.codex
cat /tmp/findb-skill/findb-api/SKILL.md >> ~/.codex/instructions.md

# references / assets 留在固定位置供 @-mention 拉細節
mkdir -p ~/.codex/skills/findb-api
cp -r /tmp/findb-skill/findb-api/* ~/.codex/skills/findb-api/`}
        />
      </div>
      <div>
        <h3 className="mb-3 font-bold">
          方法 B：寫進專案 AGENTS.md（限該 repo）
        </h3>
        <CodeBlock
          label="shell"
          code={`cd /path/to/client-project
mkdir -p docs/findb-skill
unzip /path/to/findb-api.skill -d docs/findb-skill

cat >> AGENTS.md <<'EOF'

## FinDB API Integration

Authoritative API reference: docs/findb-skill/findb-api/SKILL.md.
Endpoint specs / payload shapes / errors in docs/findb-skill/findb-api/references/.
Staging base URL: https://findb.tingfong.com.
EOF

git add AGENTS.md docs/findb-skill
git commit -m "Add FinDB API skill as project reference"`}
        />
      </div>
    </div>
  )
}

function CursorInstructions() {
  return (
    <div className="space-y-4">
      <p className="text-sm leading-7 text-muted">
        SKILL.md 是純 Markdown，能放進任何接受 system prompt 或專案規則的工具。
      </p>
      <ul className="grid list-disc gap-3 pl-5 text-sm leading-7 text-muted">
        <li>
          <strong className="text-ink">Cursor：</strong>把 SKILL.md
          內容貼進專案根目錄{" "}
          <code className="rounded bg-surface-soft px-1.5 py-0.5 font-mono">
            .cursorrules
          </code>
          ，或 Settings → Rules for AI。
        </li>
        <li>
          <strong className="text-ink">Cline / Continue：</strong>
          解壓到 repo 內，於 system prompt 引用相對路徑。
        </li>
        <li>
          <strong className="text-ink">自建 agent / Anthropic SDK：</strong>把
          SKILL.md 全文放進 system message；references/ 透過 RAG 或 file tool
          提供。
        </li>
        <li>
          <strong className="text-ink">OpenAI / Gemini SDK：</strong>
          同上，當作 system instructions。
        </li>
      </ul>
    </div>
  )
}

function ManualInstructions() {
  return (
    <div className="space-y-5">
      <p className="text-sm leading-7 text-muted">
        只想單次對話使用，不想做任何設定：
      </p>
      <CodeBlock label="shell" code="unzip findb-api.skill -d ./findb-skill" />
      <p className="text-sm leading-7 text-muted">
        然後在 AI 對話直接 @ 引用：
      </p>
      <CodeBlock
        label="prompt"
        code={`@./findb-skill/findb-api/SKILL.md @./findb-skill/findb-api/references/endpoints.md
請幫我寫一個從 staging FinDB 拉 AAPL EOD 的 Python 腳本`}
      />
    </div>
  )
}

function TabPanel({
  id,
  activeTab,
  labelId,
  children,
}: {
  id: TabId
  activeTab: TabId
  labelId: string
  children: ReactNode
}) {
  return (
    <div
      id={`${labelId}-panel-${id}`}
      role="tabpanel"
      aria-labelledby={`${labelId}-tab-${id}`}
      tabIndex={0}
      hidden={activeTab !== id}
      className="pt-6 outline-none focus-visible:ring-3 focus-visible:ring-accent/20"
    >
      {children}
    </div>
  )
}

export default function InstallTabs() {
  const [activeTab, setActiveTab] = useState<TabId>("claude")
  const tabButtons = useRef<Array<HTMLButtonElement | null>>([])
  const labelId = useId().replaceAll(":", "")

  function activateTab(id: TabId, focus = false) {
    setActiveTab(id)
    if (focus) {
      const index = tabs.findIndex(tab => tab.id === id)
      tabButtons.current[index]?.focus()
    }
  }

  function handleKeyDown(event: KeyboardEvent<HTMLButtonElement>) {
    const currentIndex = tabs.findIndex(tab => tab.id === activeTab)
    let nextIndex: number | null = null

    if (event.key === "ArrowRight") nextIndex = (currentIndex + 1) % tabs.length
    if (event.key === "ArrowLeft")
      nextIndex = (currentIndex - 1 + tabs.length) % tabs.length
    if (event.key === "Home") nextIndex = 0
    if (event.key === "End") nextIndex = tabs.length - 1

    if (nextIndex === null) return
    const nextTab = tabs[nextIndex]
    if (!nextTab) return

    event.preventDefault()
    activateTab(nextTab.id, true)
  }

  return (
    <div>
      <div
        className="flex gap-1 overflow-x-auto border-b border-line"
        role="tablist"
        aria-label="AI 開發工具安裝方式"
      >
        {tabs.map((tab, index) => (
          <button
            key={tab.id}
            id={`${labelId}-tab-${tab.id}`}
            ref={element => {
              tabButtons.current[index] = element
            }}
            type="button"
            role="tab"
            aria-selected={activeTab === tab.id}
            aria-controls={`${labelId}-panel-${tab.id}`}
            tabIndex={activeTab === tab.id ? 0 : -1}
            onClick={() => activateTab(tab.id)}
            onKeyDown={handleKeyDown}
            className={
              activeTab === tab.id
                ? "shrink-0 border-b-2 border-accent px-3 py-3 text-sm font-bold text-accent outline-none focus-visible:ring-3 focus-visible:ring-accent/20"
                : "shrink-0 border-b-2 border-transparent px-3 py-3 text-sm font-bold text-muted outline-none hover:text-ink focus-visible:ring-3 focus-visible:ring-accent/20"
            }
          >
            {tab.label}
          </button>
        ))}
      </div>
      <TabPanel id="claude" activeTab={activeTab} labelId={labelId}>
        <ClaudeInstructions />
      </TabPanel>
      <TabPanel id="codex" activeTab={activeTab} labelId={labelId}>
        <CodexInstructions />
      </TabPanel>
      <TabPanel id="cursor" activeTab={activeTab} labelId={labelId}>
        <CursorInstructions />
      </TabPanel>
      <TabPanel id="manual" activeTab={activeTab} labelId={labelId}>
        <ManualInstructions />
      </TabPanel>
    </div>
  )
}
