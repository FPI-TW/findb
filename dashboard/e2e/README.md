# Dashboard E2E 測試

執行 `pnpm test:dashboard:e2e`（或在此套件內執行 `pnpm test:e2e`）會由 Playwright 啟動 Dashboard 開發伺服器與 `fixtures/operations-backend.ts`。

Operations 測試透過正常登入頁、HttpOnly session cookie、驗證登入狀態的 Server Functions 與排程 CAS 更新路徑進行驗收。測試後端只綁定 `127.0.0.1:18081`；Dashboard 測試伺服器使用 `FINDB_API_BASE_URL` 指向該後端，不連線到真實 FinDB，也不修改真實排程。Fixture 回傳值由 Dashboard 現行 Zod schemas 驗證。

`/fixture/reset` 與 `/fixture/state` 只存在於 Playwright 啟動的獨立測試程序，提供每個案例的資料重設與 mutation 紀錄；不屬於 Dashboard 路由或正式後端。正式應用程式沒有測試登入旁路。Operations 案例在同一 spec 依序執行，不應啟用該 spec 的平行執行，避免共用 fixture 狀態互相干擾。

涵蓋預設 Pilot、Full market 深連結、瀏覽器上一頁／下一頁與重新整理、兩分頁執行中數量、Full market readiness／凍結範圍／首次日期、部署前停止兩組提醒、隱藏 counterpart 重疊警告、取消與 Escape 無副作用、批次啟停的 profile 隔離與 revision、部分略過／全部阻擋，以及非 Owner 權限限制。元件瀏覽器測試另驗證分頁方向鍵、Home／End、focus 與窄螢幕確認對話框。

Fixture 使用 Node.js 原生 TypeScript 執行，需要支援 type stripping 的 Node.js（專案開發環境使用 Node.js 24）。Playwright 會在結束時停止兩個測試伺服器；失敗的 trace 保存在 `.playwright-results/`。
