# 港股個股日線：FactSet 供應、HKEX 官方值優先

> 狀態：已定事項由 Owner 於 2026-11-20 確認，尚未實作。追蹤 issue：
> [#610](https://github.com/FPI-TW/findb/issues/610)。FactSet 港股的現行契約與操作見
> [FactSet 港股日行情](../operations/factset_hk.md)；首次環境啟用的待辦見 [backlog](backlog.md#factset-港股首次環境啟用)。
> 完成後依 `docs/dev/` 的規則，把仍有效的內容整併到 `docs/` 的現行文件並刪除本檔。

## 已定事項

- **港股個股日線的正式來源是 FactSet。** `hk_equity_eod` 的 Source feed 維持 FactSet。
  - 2026-11-19 曾決定改由 FMP 供應，2026-11-20 依「三方比對」改回。
  - FMP 不承接港股個股，見 [FMP provider 導入規劃](fmp-provider.md)。
- **HKEX 官方值優先。** 接上 HKEX 每日報表後每天核對，不一致以 HKEX 為準：
  - HKEX 為獨立的 provider `hkex` 與 dataset，以欄位層修正 FactSet 的港股列，開盤價保留 FactSet 的值。
  - FactSet 沒有的港股標的，第一版只存在 HKEX 的表。
  - 原始報表長期歸檔到 Canonical R2，本機已下載的報表一併歸檔。
- 2026-11-21 Owner 確認三項實作決定：
  - 欄位層修正記在兩處：`market_data_eod` 新增 `field_sources`（例如 `{"close":"hkex"}`），另有一張只增不改的
    修正紀錄表，存每次修正前後的值與 run；差異超過門檻才另記 DQ warning。
  - HKEX 報表上停牌或暫停交易的成員，記為 plan 的 `no_data`，證據是報表的 R2 位置、SHA-256 與原文那一行；
    不寫入官方表。
  - HKEX dataset 自己更新名單：下載 ListOfSecurities，篩選規則與 FactSet 相同，但不需要 FactSet 對應。
- 範圍只到「每日核對與修正港股原始價」。VWAP 的計算與 Serve 欄位仍列在 [backlog](backlog.md#vwap)；成交金額在這一階段
  就一併存下，之後 VWAP 不必重抓。

## 三方比對

2026-09-21 至 11-18 共 41 個交易日，以 HKEX 每日報表（主板加 GEM）為官方值，比對 FactSet
（`fgp_v1_fgp_global_prices`，查法與 Fetcher 相同）與 FMP（`historical-price-eod/non-split-adjusted`）。只算 FinDB
港股名單（HKEX `ListOfSecurities` 的 Equity 列）內、HKD、HKEX 當天有成交的列：90,586 列、2,676 檔。HKEX 報表沒有
開盤價，開盤價無法比對。

| 項目 | FactSet | FMP |
| --- | ---: | ---: |
| 有資料的列 | 99.84% | 99.21% |
| 有資料的列，以成交金額計 | 99.97% | 99.60% |
| 收盤、最高、最低、成交量全部與 HKEX 相同（列） | **99.84%** | 36.1% |
| 承上，以成交金額計 | **99.97%** | 40.7% |
| 容許 0.005 價差與 1% 量差時全部相符，以成交金額計 | 99.97% | 98.8% |
| HKEX 當天沒成交或停牌，卻有成交量的列 | 2 | 88 |
| HKEX 有成交，成交量卻是 0 的列 | 0 | 576 |

- **FactSet 幾乎與 HKEX 相同。** 兩邊都有資料的 89,846 列中只有 2 列不同：0653 在 09-30 的收盤價（HKEX 1.41、
  FactSet 1.47），以及 1560 在 10-13 的成交量（FactSet 少一位數）。
- **FactSet 缺的 11 檔（146 列）都不是一般普通股**：預託證券 2 檔（6228、6288），其餘是併股期間的臨時平行櫃台
  （名稱帶 `-OLD`）、供股權等。FMP 缺 30 檔，包含一般股票（例如 1688、2672）。
- **FactSet 的 `turnover` 單位是千港元**（約為 HKEX 的 1/1000，四捨五入），不寫入 canonical 的成交金額；成交金額由
  HKEX 提供。
- FMP 的問題：價格只留 2 位小數、冷門股成交量偏多或為 0、公司行動前後價量錯誤，細節見
  [FMP provider 導入規劃](fmp-provider.md#港股與-hkex-官方值比對)。
- 2026-11-20 09:54（台北）時 FactSet 已有 11-19 的資料；兩邊都有的 2,032 檔，收盤價與成交量全部與 11-19 的 HKEX
  報表相同。FactSet 當晚幾點可取得尚未測量。
- 不限 FinDB 名單時，FactSet 的涵蓋率只有成交金額的 80.7%：查詢只取普通股，不含 ETF 與槓桿／反向產品（例如
  2800、3033）。這些產品不在 FinDB 的港股名單內。

## HKEX 每日報表

### 報表事實

- 網址形如 `https://www.hkex.com.hk/eng/stat/smstat/dayquot/d<yymmdd>e.htm`，公開、不需帳號。
  - 每個交易日一個檔，約 22–24 MB，gzip 後約 3.5 MB。
  - 涵蓋全部港股：前收、最高、最低、收盤、買賣價、成交股數、成交金額；停牌以狀態文字標示
    （11-18 有 362 檔為 `TRADING SUSPENDED`）。
  - 11-18 共 16,914 列，其中 13,408 列是代號 10000 以上的權證與牛熊證；代號 1–9999 約 3,500 列。
  - **沒有開盤價。**
- **主板報表不含 GEM。** GEM 另有報表 `https://www.hkex.com.hk/eng/stat/smstat/dayquot/GEM/e_G<yymmdd>.htm`，
  約 110 KB，產生時間與保留期間相同。港股名單（2026-11-20）共 2,811 檔：主板 2,476、GEM 306、未分類 29，
  所以每天要抓兩份報表。兩份的段落與每檔兩行的格式相同，同一個解析器可用。
- **每個交易日香港時間 20:00 產生、約 21:00 才能下載。** 2026-09-21 至 11-18 共 41 份報表的 `Last-Modified`
  全部落在當天 20:00，但實際可下載的時間晚約一小時，見「發布時間」。週末與發布前回 404，假日回內容為
  「NO TRADING」的頁面。
  - 假日頁是所有假日共用的通用頁面（ETag 相同、`Last-Modified` 為 2017 年），內容沒有日期；作為 no_data 證據時
    要搭配請求的網址與交易所日曆。
- **只保留約兩個月**：2026-09-21 以後可取，更早的回 404。上線後只能往回補到當時仍保留的日期。
- 連續 HEAD 請求曾一律回 503；GET 與 `Range` 請求正常。
- **一般 GET 時 CDN 會在 `</body>` 前插入一段每次不同的 script**（約 155 bytes），同一份報表每次下載的雜湊都不同。
  `Range: bytes=0-` 回 206、穩定的 ETag，內容就是原檔；去掉那段 script 的一般下載與原檔逐位元組相同。
- 解析：QUOTATIONS 段落，每檔兩行；幣別除 HKD 外還有 USD、CNY 等櫃台，納入範圍跟港股名單一致。
  - 代號後緊接 `#` 表示「今天起停牌或暫停交易」，例如 `690#UNI-BIO GROUP HKD TRADING HALTED`；
    狀態文字有 `TRADING SUSPENDED` 與 `TRADING HALTED`。
  - 前收為 `N/A` 多半是新上市的股票、供股權或債券；第二行 `N/A - - -` 表示沒有收盤價。
  - 有成交量卻沒有最高、最低（`-`）的列約佔有成交列的 1.3%，當天只有碎股成交。
- **Range 請求偶爾也會被 CDN 插入 script**（2026-11-21 下載 11-20 報表時遇到）：該次 206 回應沒有
  `Content-Length`，內容比 `Content-Range` 的總長多約 150 bytes。只有長度等於 `Content-Range` 總長的回應
  才是原檔，不符就重抓。

### 發布時間

**報表在香港時間 21:00 左右才能下載，不是 CDN 快取造成的。**

| 交易日 | `Last-Modified` | 最後一次 404 | 第一次取得 | 請求方式 |
| --- | --- | --- | --- | --- |
| 11-19 | 20:00:03 | 20:59:55 | 21:00:58 | 主板，每分鐘同一網址 |
| 11-20 | 主板 20:00:04、GEM 20:00:06 | 20:59:41 | 21:00:50 | 主板與 GEM，各以兩種方式每分鐘請求 |

- 兩天都在 21:00 整點前後一分鐘內才能下載。
- 11-20 的兩種方式：一種每次同一網址；一種每次帶不同的網址參數，並送 `Cache-Control: no-cache`。
  兩種方式在同一輪（21:00:50）第一次取得，主板與 GEM 也是同一輪。
- 21:00 前的 404 來自 HKEX 的網站伺服器（`X-AspNet-Version`、`Cache-Control: private`），不是 CDN 存下的回應。
  所以換網址也拿不到，`Last-Modified` 只代表檔案產生時間，不代表可下載時間。
- 取得後的內容與 `Last-Modified` 相符，主板 26,298,848 bytes、16,633 列，GEM 109,951 bytes、306 檔。
- 2026-11-19 Owner 決定「預設認定是快取所致」，11-20 的觀測推翻了這個假設。因此排程改為香港時間 21:05 開始，
  見下方 Backend。

### 合併方式

`market_data_eod` 一列只來自一個來源、依優先序整列取捨；HKEX 沒有開盤價，不能整列取代 FactSet 的列，而且
「每個 dataset 只能有一條 Source feed」。因此：

- HKEX 是獨立的 provider `hkex` 與 dataset（暫稱 `hk_equity_official_eod`），寫入自己的 canonical 表，以
  `(instrument_id, trade_date)` 為鍵，存報表的全部數值欄位與交易狀態。
- **欄位層修正**：同一交易日兩邊都有資料時，Backend 以 HKEX 的收盤、最高、最低、成交量、成交金額覆寫
  `market_data_eod` 的港股列，開盤價保留 FactSet 的值。
  - 不論誰先寫入結果都相同：HKEX 寫入時修正既有的 FactSet 列；FactSet 重抓或補抓時，若同日已有 HKEX 值，寫入後
    再套用一次。
  - 被取代的 FactSet 原值記在只增不改的 `eod_field_correction`，列上的 `field_sources` 註明哪些欄位來自
    HKEX；HKEX 為 null 的欄位保留 FactSet 的值，人工修正過的列不動。規則見
    [ingress contract](../architecture/ingress_contracts.md#修正-market_data_eod-的港股列)。
  - 差異超過門檻（收盤價不同，或成交量差超過 1%）另開 warning，供監控 FactSet 品質；依三方比對，預期每月只有
    個位數。
- **HKEX 有、FactSet 沒有的標的**：第一版只存在 HKEX 的表，不在 `market_data_eod` 新增開盤價為空的列。
- **HKEX 停牌或無成交的日期**：以 HKEX 的狀態為準，FactSet 該日若有成交量，記為差異。

### Backend

- **需要新 contract**（例如 `exchange_daily_quote` v1）。Normalizer 依 contract 的 schema 與版本路由，沿用
  `market_eod` v2 會寫進 `market_data_eod`，不會寫進 HKEX 自己的表。contract 帶交易狀態、前收、成交金額等欄位，
  batch 帶 `source_raw_ref`、`source_raw_sha256`、報表的 `Last-Modified` 與 ETag。
- 新 canonical 表、歸檔索引表、normalizer、上述欄位層修正、DQ。DQ 至少包括：
  - 成交股數為 0 時不得有成交金額；最高不得低於最低。兩者是 error。
  - 成交金額 ÷ 成交股數落在最低與最高之外只記 warning：最高、最低不含碎股與特別買賣，09-21 至 11-20 有成交的
    主板列約 1.3%、GEM 約 3.3% 落在範圍外，主板有 3 列偏離超過 100%（例如供股期間），所以無法用來驗證解析。
  - 不檢查收盤價是否落在最低與最高之間：HKEX 的收盤價常不是成交價，每天約 3.4% 的有成交港股落在範圍外。
    `market_data_eod` 的港股列同樣只記 warning（見 [`market_eod.v2`](../architecture/ingress_contracts.md#market_eodv2)）。
- Catalog 新增 provider `hkex` 與 feed；`calendar_market` 為 HK。
  - 排程：香港時間 21:05 起（報表約 21:00 才能下載，見「發布時間」），404 視為尚未發布並重試到當晚。
  - 完成期限固定為排程加 12 小時（`backend/app/provider_catalog.py` 的 `COMPLETION_DEADLINE_HOURS_AFTER_SCHEDULE`），
    也就是隔天 09:05。FactSet 港股排程於 2026-11-20 改為 20:00、隔天 08:00 期限，比 HKEX 早一小時；
    合併結果與寫入順序無關（見「合併方式」）；
    FactSet 原有的 18:30–23:00 專屬窗口已移除，沿用全 provider 的期限規則。
  - 假日頁以「NO TRADING」作為 no_data 的 durable evidence。
- Registry、control、Source credential 與 migration 的做法比照現有 provider。新增 control 時要一併更新部署前
  執行的凍結 schedule inspection（`scripts/preflight_catalog_schedule.py`），否則會判為 `unknown_difference`、
  中止部署。

### Fetcher

- 比照 TAIFEX：官方報表、沒有 credential、沿用 `image_key: twelve_data`。
- **抓取用自己的函式**，不走 `context.snapshot`：後者只接受 HTTP 200，Range 請求回 206。
  - 每次請求帶 `Range: bytes=0-`、不重複的網址參數與 `Cache-Control: no-cache`。
  - 回應約 24 MB，超過現行 provider 的 `max_response_bytes`（16 MiB），`hkex` 需設較高的上限。
- **原檔先 gzip 再存 Raw R2。** Raw R2 物件上限是 16 MiB（`fetcher/src/findb_fetcher/raw_storage.py`），主板報表
  放不下；以 `mtime=0` 壓縮，壓縮結果可重現，約 3.5 MB。需要新的 raw 格式（例如 `html_gzip`）。
  - contract 帶 `source_raw_ref` 與 `source_raw_sha256`，資料列只帶名單範圍內解析後的列。
  - `raw.market_payload` 只存 contract，不存 HTML。
  - Fetcher 的 durable response journal 也只存壓縮後的內容。
- 每檔一個 request，與現行做法相同。
- 實作（第 3 個 PR，`fetcher/src/findb_fetcher/providers/hkex.py`、`provider_adapters/hkex.py`）：
  - 只收「206、`Content-Range` 從 0 開始且總長等於內容長度」的回應，否則同一輪內最多重抓 3 次；
    伺服器忽略 Range 回 200 時也重抓，因為無法證明沒有被插入 script。404 視為尚未發布
    （`wait="publication"`）；408／429／5xx、連線錯誤與持續被改寫的回應是全 plan 共用的 outage；
    超過回應上限、其他 4xx 與格式錯誤讓當天 work 失敗。
  - 解析器很嚴格：每檔兩行的行情、一行的停牌或暫停交易、空行以外的任何一行都讓整份報表被拒絕，
    並核對報表標頭的 `DATE:` 等於請求的交易日。本機 2026-09-21 至 11-20 的 90 份報表（主板與 GEM
    各 45 份）共 725,970 列行情、18,053 列停牌或暫停交易、4 份假日頁，全部解析成功、沒有未知格式；
    Python `gzip`（level 9、`mtime=0`）的壓縮結果與本機歸檔逐位元組相同。
  - 每份報表只抓一次、只存一個 Raw R2 物件（`daily_quotation_<board>`、格式 `html_gzip`、
    `application/gzip`，不設 Content-Encoding），所有成員共用。GEM 成員先查 GEM 報表。
  - 成員結果：報表有列就送 `data`（無成交送 `no_trade` 列）；停牌或暫停交易回報 no_data、reason
    `halted`，證據是報表網址、Raw R2 物件的 SHA-256 與該行原文；假日頁回報 no_data、reason
    `no_trade`；名單有、兩份報表都沒有的成員是 `missing`。為此 `AcquisitionResult` 加了
    `no_data_reason`，runtime 不再固定送 `no_trade`。
  - 名單：`ListOfSecurities` 的代號就是報表的代號，成員一律 `mapped`；HKEX runtime 把名單快照存在
    自己的 dataset 底下（`official_snapshot_dataset` 依 runtime 安裝的 feed 決定）。
  - `hkex` 加入 journal 清理的 `_R2_BACKED_RESPONSES`：journal 存的就是上傳 Raw R2 的 gzip。
  - 設定 `HKEX_REPORT_ARCHIVE_DIR` 時改讀本機歸檔（`reports/`、`gem_reports/` 與兩份 manifest），
    並以 manifest 的 SHA-256 核對原檔，供第 5 個 PR 匯入 HKEX 已刪除的日期；historical 沿用通用的
    `MemberHistoricalRunner`，它直接呼叫 adapter 的 `acquire`。
  - adapter 尚未註冊到 `ADAPTERS`、CLI 與 `_IMPLEMENTED_ADAPTERS`：Fetcher 的抓取要讀 catalog 的
    平台上限，catalog 沒有 `hkex` 時一律拒絕，所以測試暫時注入一份 `hkex` 的 provider 描述。

### 原始報表歸檔

Owner 於 2026-11-19 確認長期歸檔。HKEX 兩個月後就刪除報表，Raw R2 的 lifecycle 是 90 天，都不是長期保存；
歸檔後才能事後重新解析或擴大範圍（例如權證與牛熊證）。

- **放在 Canonical R2，只能由 Worker 寫入。** 依 [服務邊界](../architecture/overview.md)，Fetcher 只持有同環境
  Raw R2 的 publisher credential；Worker 持有 Raw reader 與 Canonical publisher。
- 做法比照盤中資料（[intraday storage](../architecture/intraday_storage.md)）：HKEX normalizer 以
  `RawObjectReader` 讀 Raw R2 的 gzip、核對 SHA-256，直接複製到 Canonical R2（不必重新壓縮），再在同一個
  transaction 寫官方表與歸檔索引。物件先寫、索引後寫；重送相同雜湊時不重寫物件。目前沒有 normalizer 讀取
  `source_raw_ref` 的物件，HKEX 是第一個。
- 每份約 3.5 MB、一年約 1 GB；索引記交易日、object key、ETag、原檔與 gzip 的 SHA-256、HKEX 的 `Last-Modified`
  與 ETag。假日頁也歸檔，作為 no_data 的證據。
- 路徑定為 `archive/v1/hkex/daily_quotation/board=<main|gem>/year=<YYYY>/<YYYYMMDD>/<gzip SHA-256>.htm.gz`：
  以 gzip 內容命名，物件寫入後不再改變；HKEX 重新發布的報表是另一個物件與索引列。
- **歸檔每份報表只做一次**：以 SHA-256 去重，第一個引用該報表的成員觸發歸檔，之後的成員只核對索引。

### 交付量與耗時

依 2026-11-18 的報表與名單估算：

| 項目 | 量 |
| --- | --- |
| Fetcher → Raw R2 | 每天 2 個物件（主板 gzip 約 3.5 MB、GEM 約 0.03 MB），各上傳一次 |
| Fetcher → Source | 每檔一個 request，約 1 KB；每天約 2,810 個，合計約 3 MB |
| Fetcher 解析與雜湊 | 主板報表解析約 0.05 秒、SHA-256 約 0.01 秒 |

- **耗時主要在每檔的 Source 往返，不在資料量。** 現行每檔約 16 次 Source RPC；HKEX 一天只下載一次報表，每檔約
  1.0–1.1 秒，2,810 檔約 50 分鐘：21:05 開始，約 22:00 完成。
- 欄位層修正每檔多一次 `market_data_eod` 更新，在毫秒等級。
- 改成整天一個 request 可以縮到數秒，但會改變現行「每檔各自結算、各自留證據」的完整性模型，不在本階段範圍。

### 歷史

- 上線時能補到的最早日期是「上線日往前約兩個月」，更早的港股歷史維持 FactSet 的值。
- **本機已下載的報表一併歸檔**（Owner 於 2026-11-19 確認）：2026-09-21 至 11-20 的 43 個交易日，加上 10-01、
  10-19 兩份假日頁；主板與 GEM 各 45 份，已逐份核對為 HKEX 原檔，保存在不進版控的位置。11-19、11-20 於
  11-21 補入；上線前每個交易日都要照此補入。
  - 匯入走同一條路徑：Fetcher 以一次性的本機檔匯入寫進 Raw R2、送 Source，由 Worker 歸檔並寫入官方表。
  - 09-21 的報表約 11-21 起陸續被 HKEX 刪除，本機副本必須保存到匯入完成。

## 實作進度

| PR | 內容 | 狀態 |
| --- | --- | --- |
| 1 | contract `exchange_daily_quote.v1`、官方表 `exchange_daily_quote`、歸檔索引 `exchange_report_archive`、normalizer 與 Canonical R2 歸檔（見 [ingress contract](../architecture/ingress_contracts.md#exchange_daily_quotev1)）；尚無 dataset 接受 | 已合併（#622） |
| 2 | FactSet 港股列的欄位層修正：`market_data_eod.field_sources`、修正紀錄表 `eod_field_correction`、同交易日 advisory lock 讓寫入順序無關、差異 DQ warning | 本 PR |
| 3 | Fetcher adapter：Range 抓取與 script 檢查、gzip 存 Raw R2、主板與 GEM 解析、名單、停牌 no_data 證據、假日頁、本機歸檔讀取；journal 清理加入 `hkex`（見上方「Fetcher」的實作） | 本 PR |
| 4 | 啟用：catalog provider 與 feed、dataset `hk_equity_official_eod`（defaults 須為 `market` HK、`asset_class` equity，才會與 FactSet 寫到同一個 instrument）、control、凍結排程檢查、Source credential、infra consumers 與 secrets；classify plan 支援官方表。另外：`max_response_bytes` 至少 32 MiB（主板報表約 26 MB，Fetcher 上限 64 MiB）、slot 用 `asia_pacific_markets_window`（contract 的 `delivery.slot_id` 是列舉）、註冊 `ADAPTERS`／`_IMPLEMENTED_ADAPTERS`／historical runner，並移除測試注入的 provider 描述 | 未開始 |
| 5 | 匯入本機報表、staging 驗收、文件整併 | 未開始 |

每個 PR 合併後都能單獨部署：catalog 一旦列出 provider，部署就要求它的 Source credential 與兩個 consumers，
所以 provider 與 dataset 到第 4 個 PR 才一起加入。

## 待決事項

- FactSet 港股每日資料的可取得時間：loader 稽核顯示亞太價格約在香港時間 20:30 整批入庫，2026-11-20
  起逐次觀測確認（見 [FactSet runbook](../operations/factset_hk.md#重試層級與來源更新時間)）。
- 港股的調整因子來源（FactSet 或 FMP），見 [還原價：調整因子表設計](price-adjustment-factors.md)。
