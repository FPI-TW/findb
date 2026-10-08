# Dashboard E2E 測試

執行 `pnpm test:dashboard:e2e`（或在此套件內執行 `pnpm test:e2e`）會由 Playwright 啟動 Dashboard 開發伺服器與 `fixtures/operations-backend.ts`。

Operations 測試透過正常登入頁、HttpOnly session cookie、驗證登入狀態的 Server Functions 與排程 CAS 更新路徑進行驗收。測試後端只綁定 `127.0.0.1:18081`；Dashboard 測試伺服器使用 `FINDB_API_BASE_URL` 指向該後端，不連線到真實 FinDB，也不修改真實排程。Fixture 回傳值由 Dashboard 現行 Zod schemas 驗證。

`/fixture/reset` 與 `/fixture/state` 只存在於 Playwright 啟動的獨立測試程序，提供每個案例的資料重設與 mutation 紀錄；不屬於 Dashboard 路由或正式後端。正式應用程式沒有測試登入旁路。Operations specs 共用 fixture 後端，Playwright 固定單 worker 依序執行，避免不同 spec 的 reset、角色與計數互相干擾。

涵蓋預設 Pilot、Full market 深連結、瀏覽器上一頁／下一頁與重新整理、兩分頁執行中數量、Full market readiness／凍結範圍／首次日期、部署前停止兩組提醒、隱藏 counterpart 重疊警告、取消與 Escape 無副作用、批次啟停的 profile 隔離與 revision、部分略過／全部阻擋，以及非 Owner 權限限制。元件瀏覽器測試另驗證分頁方向鍵、Home／End、focus 與窄螢幕確認對話框。

Fixture 使用 Node.js 原生 TypeScript 執行，需要支援 type stripping 的 Node.js（專案開發環境使用 Node.js 24）。Playwright 會在結束時停止兩個測試伺服器；失敗的 trace 保存在 `.playwright-results/`。

交付監控案例另涵蓋三分頁 URL／瀏覽器歷史、完整資料集選項、分頁與草稿隔離、viewer 限制，
以及三次輪詢與手動刷新時表格／篩選列位移不超過 1 CSS pixel、焦點／展開／捲動保留、失敗恢復與窄螢幕。
`/fixture/delivery-config` 僅在本機 fixture 提供延遲／失敗注入，不屬於正式 API。

回補驗收對 operator 與 owner 各自使用正常登入，檢查告警帶入的 provider／dataset／起訖日期、
預覽無寫入、確認 gating、建立的完整 payload 只送出一次，以及刷新後紀錄可見。
切換分頁、手動刷新與 60 秒背景更新均須保留草稿／預覽／確認；修改參數或改帶另一告警必須
取消授權，延遲的舊預覽不能恢復授權。另涵蓋本地 session 失效與 upstream 401，確認編輯器與
scope 選項消失並返回登入頁。概況部分來源失敗／恢復時，卡片、操作列與佇列位移不得超過 1 CSS pixel。
`/fixture/backfill-config`、`/fixture/session` 與 `/fixture/overview-config` 僅供 loopback fixture
注入延遲、失效與部分服務故障。Fixture 的預覽與建立回應套用正式 Zod schemas，
`/fixture/state` 分別記錄預覽及寫入 payload，GET 回補紀錄回傳已建立請求；不執行真實回補。

SSR 分頁互動契約：`OperationsTabs` 在 SSR 與初次 hydration render 保留選取狀態、
ARIA tab／panel 關係與內容，但按鈕 disabled 且 tabindex=-1；hydration 後才恢復互動與 roving focus。
交付監控的 owner reload／viewer 深連結案例由 Playwright 暫停真實分頁模組回應，
先驗證 SSR 尚不可互動，再釋放回應並等待按鈕 enabled。第一個點擊或 ArrowRight 必須
立即切換告警分頁、更新 URL 與鍵盤焦點，不以 SSR 資料可見、load 完成、固定 sleep 或重複操作
判定就緒；同時檢查無 hydration 錯誤、viewer 無回補讀取／寫入，以及既有歷史與草稿行為。
延遲控制僅存在於測試側 resource routing，正式程式不提供測試 hook。
