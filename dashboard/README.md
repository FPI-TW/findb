# FinDB Dashboard

TanStack Start 前端，整合公開的標的查詢，以及需登入的唯讀營運台。

## Routes

| Route                                | 權限   | 用途                           |
| ------------------------------------ | ------ | ------------------------------ |
| `/dashboard/`                        | 公開   | 首頁與功能入口                 |
| `/dashboard/lookup`                  | 公開   | Active金融商品查詢             |
| `/dashboard/login`                   | 公開   | 操作人員登入                   |
| `/dashboard/change-password`         | 需登入 | 首次登入強制改密碼             |
| `/dashboard/operations`              | 需登入 | 佇列與 Worker 健康概況         |
| `/dashboard/operations/deliveries`   | 需登入 | 全市場交付計畫、缺漏交付與回補 |
| `/dashboard/operations/quality`      | 需登入 | 未解決 DQ 問題                 |
| `/dashboard/operations/corrections`  | 需登入 | 修正稽核紀錄                   |
| `/dashboard/operations/raw-payloads` | 需登入 | Raw payload 稽核查詢           |
| `/dashboard/operations/credentials`  | 需登入 | API credential 治理            |
| `/dashboard/operations/users`        | Owner  | 管理者帳號與角色               |

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

公開 Lookup 透過同源的`/api/v1/serve/instruments`取得列表、全域facets與分頁資料，並依每筆
`coverage.eod`／`coverage.minute`載入`/api/v1/serve/eod`與`/api/v1/serve/minute`。
`asset_class=future`的商品詳情使用`/api/v1/serve/futures/eod`，依商品、合約、日期及
日盤／夜盤查詢各合約行情，並使用cursor分頁；商品coverage的`latest_close`為空時不推算
任一合約收盤價。舊
`?ds=macro`書籤會正規化回instruments。前端測試只mock此HTTP contract，不讀取backend source或
generated cache；列表只會包含active dataset scopes。

交付頁透過登入session的server function讀取`/api/v1/admin/delivery-plans`，顯示各批次
預期、有資料、正常無資料、缺漏、受阻與逾時狀態；讀取失敗會顯示錯誤，不當作空計畫。
計畫面板僅供查閱，不提供啟用或發布操作。

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
pnpm test:dashboard
pnpm build:dashboard
```

`check:dashboard` 只執行 format、lint 與 type check。`test:dashboard` 會依序執行 unit、
Chromium browser component 與 e2e；需要分開執行時，使用
`pnpm test:dashboard:unit`、`pnpm test:dashboard:browser` 或
`pnpm test:dashboard:e2e`。browser component 會驗證 DataTable 的 overflow、sticky header
與 pinned columns、導入排程分頁鍵盤操作與窄螢幕確認對話框；e2e 會啟動目前 checkout 的
本機 Dashboard，檢查公開 Lookup route 的 SSR hydration、canonical URL，以及透過正常
登入驗證的 Operations 分頁與批次控制。E2E 使用 loopback fixture backend，不修改真實
排程，且不會重用已在 port 3000 執行的 server；fixture 與案例說明見
[e2e/README.md](e2e/README.md)。

目前後端沒有歷史 ingestion run 趨勢端點，因此 dashboard 只呈現即時 queue/worker health。Raw payload 搜尋結果也受後端 retention policy 限制。
