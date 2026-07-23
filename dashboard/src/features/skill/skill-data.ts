export const skillArchive = {
  filename: "findb-api.skill",
  format: "zip",
  size: "~42 KB",
  fileCount: 5,
}

export const skillFiles = [
  { name: "├── SKILL.md", size: "12 KB" },
  { name: "├── assets/", directory: true },
  { name: "│   └── sample_payload.json", size: "3 KB" },
  { name: "└── references/", directory: true },
  { name: "    ├── endpoints.md", size: "13 KB" },
  { name: "    ├── ingest-payloads.md", size: "9 KB" },
  { name: "    └── responses.md", size: "6 KB" },
]

export const skillContents = [
  {
    name: "SKILL.md",
    role: "主檔",
    primary: true,
    description:
      "AI 開發工具讀進後做為主指令使用。涵蓋認證模型、Serve 常用查詢範例、Source 兩種 payload 格式、分頁與錯誤碼速查。",
    tags: [
      "認證",
      "Serve 範例 ×5",
      "Source payload",
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
    description: "各市場 payload skeleton 與 FinLab / Bloomberg 來源差異。",
    tags: ["CRYPTO", "US", "FX", "HK+CN", "TW", "WTX", "MACRO"],
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
    description: "可直接 POST 的標準 ingest 範例，照抄即能跑通整條鏈路。",
    tags: ["ingest", "標準格式", "複製即用"],
  },
]
