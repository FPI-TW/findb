export const skillArchive = {
  filename: "findb-api.skill",
  format: "zip",
  size: "~27 KB",
  fileCount: 5,
}

export const skillFiles = [
  { name: "├── SKILL.md", size: "8 KB" },
  { name: "├── assets/", directory: true },
  { name: "│   └── sample_payload.json", size: "1 KB" },
  { name: "└── references/", directory: true },
  { name: "    ├── endpoints.md", size: "8 KB" },
  { name: "    ├── ingest-payloads.md", size: "6 KB" },
  { name: "    └── responses.md", size: "5 KB" },
]

export const skillContents = [
  {
    name: "SKILL.md",
    role: "主檔",
    primary: true,
    description:
      "AI 開發工具讀進後做為主指令使用。涵蓋 staging 認證模型、canonical Serve 查詢、四個 Source contract、分頁與錯誤碼速查。",
    tags: [
      "認證",
      "Serve 範例 ×5",
      "Source contract",
      "分頁",
      "錯誤碼",
      "nginx Serve key 注入",
    ],
  },
  {
    name: "references/endpoints.md",
    role: "規格",
    description: "Source / Serve / Admin 完整端點規格、必要與選用參數。",
    tags: ["Source", "Serve", "Admin", "參數"],
  },
  {
    name: "references/ingest-payloads.md",
    role: "規格",
    description:
      "目前四個 active feed 的 payload skeleton；其他 canonical read model 僅供查詢，沒有 active provider feed。",
    tags: ["US EOD", "TW EOD", "TW minute", "Serve read model"],
  },
  {
    name: "references/responses.md",
    role: "規格",
    description: "回應 wrapper、HTTP 錯誤碼對照、ingest run 狀態與 DQ 嚴重度。",
    tags: ["Pagination", "HTTP", "Ingest run", "DQ"],
  },
  {
    name: "assets/sample_payload.json",
    role: "範例",
    description: "可直接送至唯一 Source ingest route 的 active contract 範例。",
    tags: ["ingest", "標準格式", "複製即用"],
  },
]
