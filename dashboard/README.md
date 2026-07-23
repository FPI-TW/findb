# FinDB Dashboard

TanStack Start 唯讀營運台，用來監控資料導入穩定度、複查完整性與正確性，以及檢索 raw payload 與修正稽核紀錄。

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

Admin key 只存在 Dashboard server 環境，瀏覽器不會收到或輸入它。操作人員需以環境設定的單一帳號與密碼登入；沒有註冊功能。登入狀態使用有期限、簽章且 `HttpOnly` 的 cookie。

本機入口是 `http://localhost:3000/dashboard/`。若要以獨立 container 啟動：

```bash
pnpm container:dashboard
```

## Checks

```bash
pnpm check:dashboard
pnpm build:dashboard
```

目前後端沒有歷史 ingestion run 趨勢端點，因此 dashboard 只呈現即時 queue/worker health。Raw payload 搜尋結果也受後端 retention policy 限制。
