# 資料導入營運台

Dashboard 透過具名 session 與 Admin API 提供監控及授權操作，不直接連線資料庫。
既有頁面 URL 保持相容，營運台依下列責任分組：

| 分組     | 頁面                             | 用途                                                               |
| -------- | -------------------------------- | ------------------------------------------------------------------ |
| 營運監控 | 導入概況                         | Pilot／Full market 排程、readiness、Owner 啟停、佇列與 Worker 健康 |
| 營運監控 | 交付監控                         | 全市場逐成員核對、資料集日期層級告警、授權歷史回補                 |
| 營運監控 | 資料品質                         | 未解決 DQ 問題及政策資訊                                           |
| 資料稽核 | 修正稽核、原始資料               | 修正 lineage 與保留中的 raw payload                                |
| 系統管理 | 交易日曆、API 憑證、管理者使用者 | 日曆治理及身份／憑證管理，依原有角色限制操作                       |

## 交付監控

入口為 `/dashboard/operations/deliveries`。最外層是獨立的「Pilot」與「Full market」分頁，
staging 預設 Pilot，production 預設 Full market；切換不寫入 controls。
各 profile 內分為計畫、缺漏告警與歷史回補；viewer 不顯示歷史回補。
計畫 expected、data、no_data、missing、blocked、缺漏清單與告警只計算所選 profile，不能跨頁混加。
尚未啟動、已有啟動日期但尚無計畫、以及已建立計畫的實際缺漏分別呈現。

- 全市場計畫：資料集下拉選單列出所有曾有計畫的資料集，不受最近筆數或交易日篩選限制；
  預設全部資料集、不限交易日、每頁 25 筆，可選 50／100 筆。
- 計畫表格比較完整性、截止時間與五種數量；展開查看 universe release、缺口成員及原因。
  `expected = data + no_data + missing + blocked`，正常無資料依後端持久化證據判定。
- 缺口明細最多列出 1,000 筆，介面同時顯示已列出筆數與總缺口數；完整性與逾時為不同概念。
- 缺漏告警追蹤資料集／日期層級的交付；「帶入回補」僅切換分頁並填入條件。
  operator／owner 仍需預覽交易日、確認與送出，才會建立回補。
- 切換分頁會保留本次進入頁面後的回補草稿；重新載入頁面不保留未提交草稿。
  手動與定期更新不改寫草稿、預覽或確認狀態；修改參數或帶入新的告警會取消先前預覽與確認，
  較晚完成的舊預覽也不能授權目前草稿。建立後由回補紀錄刷新顯示已建立請求。

URL 的 `profile=pilot|full_market`、`tab=plans|alerts|backfills`、計畫 `dataset/date/pp/pps`、告警 `p/ps`、回補 `bp/bps`
各自獨立。切換分頁、篩選及換頁可用瀏覽器上一頁／下一頁還原；新篩選會將該清單頁碼重設為 1。
兩種 profile 的計畫都是唯讀核對，不會啟動抓取，也不會自動建立回補；排程操作仍在導入概況。

## 載入、更新與失敗

初次載入顯示骨架，不提前顯示「無資料」。更新時保留最後成功結果，包括成功查得的空集合，
並在固定高度狀態列顯示更新進度。失敗顯示錯誤、最後成功時間與重試；失敗請求不推進成功時間。
新篩選無快取時使用骨架，避免把上一條件的資料誤當成新結果。

頁首「重新整理」只刷新目前頁面／分頁與必要的選項查詢。概況、DQ、全市場計畫、缺漏告警及
回補紀錄每 60 秒更新；隱藏分頁及瀏覽器背景停止輪詢，重新可見時只有超過 60 秒的資料才補刷新。
稽核、交易日曆、使用者與憑證不定時輪詢，使用手動刷新及操作後失效重取。

更新保留排序、頁碼、展開狀態與捲動；實際資料增減仍會改變列內容。概況部分來源失敗由既有
固定高度狀態列公告，不另插入提示推動卡片、操作列或佇列。查詢、mutation 或直接操作偵測到
認證失效時，立即隱藏受保護內容、取消未完成查詢並清除 operations／calendar／admin 快取，
只導向登入頁一次。後端 401 與既有 `Invalid admin credential` 403 契約會使 session cookie 失效；
一般角色不足、密碼更新要求及業務錯誤仍維持原處理。排程、日曆及回補的後端授權、revision／CAS、預覽與確認機制保持有效。

## 前端維護契約

- `OperationsRefreshProvider` 管理可見查詢的 exact keys；資料元件透過
  `useRegisterOperationsQuery(key, enabled)` 註冊，隱藏分頁同時停用查詢及註冊。
  可見性註冊只限制頁首刷新；認證監看涵蓋所有 operations／calendar／admin 查詢，
  已停用或取消註冊的請求晚到認證失效仍立即清除受保護內容，公開及其他查詢不觸發此處理。
  頁首刷新使用 `cancelRefetch: false`，與既有 in-flight 請求合併。直接操作的 catch 必須透過
  `useOperationsRefresh().reportError(error)` 回報認證錯誤；mutation 由共用 MutationCache 監看。
  Server Functions 使用 `withDashboardAuthentication` 清除 upstream 拒絕的 cookie，避免登入守衛反向導回。
- `liveQueryOptions` 統一定時更新與可見性策略；其餘查詢維持手動更新。
- `QueryStatus` 使用固定高度呈現首次載入、背景更新、錯誤及最後成功時間；
  `hasData` 根據是否有成功 response，不能以資料列數判斷。
- 表格初次載入使用共用欄位骨架；已有外部 QueryStatus 時傳入 `refreshingState={null}`，避免重複提示。
  `errorState={null}` 可將錯誤公告交給外部狀態列。表格使用穩定業務 ID，不因輪詢重設展開或頁碼。
- 概況保留聚合查詢，以具名來源保存各自的錯誤及成功時間；交付三分頁各自查詢，不因任一分頁失敗
  而覆蓋其他分頁的有效結果。未顯示的回補編輯器保留本地草稿但停止查詢。
- 新增非同步頁面時，同步測試空集合重整失敗、快取隔離、隱藏查詢及錯誤恢復。

測試方式見 [testing.md](testing.md)；全市場交付判定與操作邊界見 [full_market.md](full_market.md)。

Overview、freshness、plans、alerts 的請求與 query cache key 都包含 profile；切換時只掛載選中模式，
不以另一 profile 的上一筆資料作為 placeholder。新 runtime／Dashboard 一律明確傳入 profile；
舊版未指定 profile 的 plan API 保留 Full market 語意。
概況卡片的啟用範圍與心跳提示同樣標示所選 Pilot／Full market。尚未納入抓取範圍的 feed
若有 blocker，顯示實際原因；沒有 blocker 則顯示「尚未啟用」，不視為日曆缺漏或資料缺漏。
啟停成功後，卡片先套用 scheduler 回應，再重新取得 freshness；freshness 的 activation 只對其計算時的
revision 有效，因此重新取得前不會把「已要求執行但尚未啟用」判為設定錯誤。重新取得後若同一
revision 仍未啟用，才顯示設定錯誤。

FactSet 的 Pilot／Full market 排程卡片、批次啟動跳過提示與確認視窗會區分兩項啟動阻擋：
`full_market_baseline_missing` 顯示「尚無已發布的 FactSet HKEX baseline」，
`provider_acquisition_cutover` 顯示「尚未提交 HK provider 切換」。操作順序見
[FactSet 港股切換](factset_hk.md#backend-first-切換)。其他供應商仍使用一般 baseline 與來源切換限制訊息。

### 有效日曆與依設定略過

日曆管理同時顯示可編輯修訂與「抓取使用的日曆」來源；published revision 優先，未設定日期標示
依市場`weekend_days`顯示預設日期（內建市場預設週六日休市）且 revision 為空，draft 不影響抓取。日曆 API 查詢不寫入或自動發布。
交付計畫在各 profile 下分開顯示有效 provider 政策及「依設定略過」日期／資料集，支援日期與資料集篩選。
略過清單最多顯示最近 100 筆並顯示總數；略過不混入 expected、missing、blocked 或 no_data。
人工回補的 skipped_calendar 顯示「依設定略過」，不算成功 Source run。

交付監控頁的「供應商工作佇列」顯示跨 Pilot／Full／historical／maintenance／rerun 的 provider owner、
FIFO 位置、等待原因、round、not_before 與 heartbeat。依供應商分頁，各頁標示工作數並只列出該供應商的工作，
保留原 FIFO 順序；固定保留 Twelve Data、FinLab、Shioaji、TAIFEX、FactSet 五個供應商分頁，
即使 API 沒有回傳該供應商的未結算工作，仍顯示 `(0)` 與空清單提示。預設選取 `twelve_data`。
API 回傳的其他供應商仍可動態補入；固定清單也供 Source 憑證表單使用，FactSet 僅對應 `hk_equity_eod`。
切換供應商不影響下方的執行模式與交付功能分頁；定期或手動更新保留目前選取的供應商。
資料來自唯讀 Admin `/provider-work`，不包含 lease token；
初次回應前顯示 loading，使用既有 operations query refresh 註冊與 session query scope。
