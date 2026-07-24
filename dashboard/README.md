# FinDB Dashboard

TanStack Start 前端，整合公開的標的查詢、FinDB API Skill 安裝說明，以及需登入的唯讀營運台。

## Routes

| Route                                | 權限   | 用途                   |
| ------------------------------------ | ------ | ---------------------- |
| `/dashboard/`                        | 公開   | 首頁與功能入口         |
| `/dashboard/lookup`                  | 公開   | 金融商品與宏觀序列查詢 |
| `/dashboard/skill`                   | 公開   | Skill 下載與安裝說明   |
| `/dashboard/login`                   | 公開   | 操作人員登入           |
| `/dashboard/operations`              | 需登入 | 佇列與 Worker 健康概況 |
| `/dashboard/operations/deliveries`   | 需登入 | 缺漏交付               |
| `/dashboard/operations/quality`      | 需登入 | 未解決 DQ 問題         |
| `/dashboard/operations/corrections`  | 需登入 | 修正稽核紀錄           |
| `/dashboard/operations/raw-payloads` | 需登入 | Raw payload 稽核查詢   |

## Local development

```bash
cd ..
cp .env.example .env
pnpm setup
pnpm dev:dashboard
```

Dashboard 統一讀取 repository 根目錄的 `.env`。必填：

- `ADMIN_API_KEY`
- `DASHBOARD_USERNAME`
- `DASHBOARD_PASSWORD`
- `DASHBOARD_SESSION_SECRET`（至少 32 字元）
- `FINDB_API_BASE_URL`（本機預設 `http://localhost:8080`）

公開頁不讀取 Admin credentials。Admin key 只存在 Dashboard server
環境，瀏覽器不會收到或輸入它。操作人員需以環境設定的單一帳號與密碼登入
`/dashboard/operations`；沒有註冊功能。登入狀態使用有期限、簽章且
`HttpOnly` 的 cookie。

公開 Lookup 透過同源的 `/api/v1/serve/lookup/instruments` 與
`/api/v1/serve/lookup/macro-series` 取得列表、facets 與分頁資料。前端測試只 mock
此 HTTP contract，不讀取 backend source 或 generated cache。

直接執行 `pnpm dev:dashboard` 時，開發入口是 `http://localhost:3000/dashboard/`。若要以獨立 container 啟動：

```bash
pnpm container:dashboard
```

Dashboard image 內固定監聽 port `3333`，本機 Compose 也映射為
`http://localhost:3333/dashboard/`，因此 container 不會占用前端開發常用的
port `3000`。這兩個本機 Dashboard port 的公開 Lookup 請求會直接連到
`http://localhost:8080`；正式環境則維持同源 `/api/v1/serve/*`，由 Nginx 代理並注入
Serve key。

## Checks

```bash
pnpm check:dashboard
pnpm build:dashboard
```

目前後端沒有歷史 ingestion run 趨勢端點，因此 dashboard 只呈現即時 queue/worker health。Raw payload 搜尋結果也受後端 retention policy 限制。
