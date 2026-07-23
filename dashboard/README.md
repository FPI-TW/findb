# FinDB Dashboard

TanStack Start 唯讀營運台，用來監控資料導入穩定度、複查完整性與正確性，以及檢索 raw payload 與修正稽核紀錄。

## Local development

```bash
cp .env.example .env
pnpm install
pnpm dev
```

`FINDB_API_BASE_URL` 是 server-only 的 FinDB 後端位址，預設為 `http://localhost:8080`。請勿使用 `VITE_` 前綴放置任何 API key。

操作人員在畫面輸入既有的 Admin API key。金鑰只保留在 React 記憶體，經過 TanStack server function 轉送至固定 allowlist 內的 Admin GET endpoints，不會寫入瀏覽器儲存空間。

## Checks

```bash
pnpm generate-routes
pnpm format:check
pnpm lint:check
pnpm type:check
pnpm test
pnpm build
```

目前後端沒有歷史 ingestion run 趨勢端點，因此 dashboard 只呈現即時 queue/worker health。Raw payload 搜尋結果也受後端 retention policy 限制。
