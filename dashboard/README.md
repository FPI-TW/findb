# FinDB Dashboard

TanStack Start 前端，整合公開的標的查詢，以及需登入的唯讀營運台。

## Routes

| Route                                | 權限   | 用途                   |
| ------------------------------------ | ------ | ---------------------- |
| `/dashboard/`                        | 公開   | 首頁與功能入口         |
| `/dashboard/lookup`                  | 公開   | 金融商品與宏觀序列查詢 |
| `/dashboard/login`                   | 公開   | 操作人員登入           |
| `/dashboard/change-password`         | 需登入 | 首次登入強制改密碼     |
| `/dashboard/operations`              | 需登入 | 佇列與 Worker 健康概況 |
| `/dashboard/operations/deliveries`   | 需登入 | 缺漏交付               |
| `/dashboard/operations/quality`      | 需登入 | 未解決 DQ 問題         |
| `/dashboard/operations/corrections`  | 需登入 | 修正稽核紀錄           |
| `/dashboard/operations/raw-payloads` | 需登入 | Raw payload 稽核查詢   |
| `/dashboard/operations/credentials`  | 需登入 | API credential 治理    |
| `/dashboard/operations/users`        | Owner  | 管理者帳號與角色       |

## Local development

```bash
cd ..
cp .env.example .env
pnpm setup
pnpm dev:dashboard
```

Dashboard 統一讀取 repository 根目錄的 `.env`。Dashboard 本身只需要：

- `FINDB_API_BASE_URL`（本機預設 `http://localhost:8080`）

公開頁不讀取 Admin credentials。操作人員使用後端 DB-backed 管理者帳號登入；
後端 opaque session token 只保存在 Dashboard server 設定的 `HttpOnly`、
`SameSite=Strict` cookie，瀏覽器 JavaScript 無法讀取。Dashboard server 呼叫
Admin API 時以 Bearer token 轉送。Staging cookie 同時啟用 `Secure`；本機 HTTP
開發則停用 `Secure`，以便在 localhost 測試。

公開 Lookup 透過同源的 `/api/v1/serve/lookup/instruments` 與
`/api/v1/serve/lookup/macro-series` 取得列表、facets 與分頁資料。前端測試只 mock
此 HTTP contract，不讀取 backend source 或 generated cache。Lookup 讀取的是 canonical
read model；它不代表該資料域目前有 active provider feed。

直接執行 `pnpm dev:dashboard` 時，開發入口是 `http://localhost:3000/dashboard/`。若要以獨立 container 啟動：

```bash
pnpm container:dashboard
```

Dashboard image 內固定監聽 port `3333`，本機 Compose 也映射為
`http://localhost:3333/dashboard/`，因此 container 不會占用前端開發常用的
port `3000`。這兩個本機 Dashboard port 的公開 Lookup 請求會直接連到
`http://localhost:8080`；staging 則維持同源 `/api/v1/serve/*`，由 Nginx 代理並注入
Serve key。

## Checks

```bash
pnpm check:dashboard
pnpm --filter dashboard test:browser
pnpm --filter dashboard test:e2e
pnpm build:dashboard
```

`test:browser` 會在 Chromium 中驗證 DataTable 的 overflow、sticky header 與
pinned columns；`test:e2e` 會啟動本機 Dashboard，檢查公開 Lookup route 的 SSR
hydration 與 canonical URL。

目前後端沒有歷史 ingestion run 趨勢端點，因此 dashboard 只呈現即時 queue/worker health。Raw payload 搜尋結果也受後端 retention policy 限制。
