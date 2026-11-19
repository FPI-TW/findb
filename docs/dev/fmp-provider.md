# FMP（Financial Modeling Prep）provider 導入規劃

> 狀態：階段 1 盤點與港股 HKEX 比對已完成（2026-11-19）；已定事項由 Owner 確認，其餘列在「待決事項」，尚未開始實作。追蹤 issue：
> [#606](https://github.com/FPI-TW/findb/issues/606)。非交易日資料的共用設計與 FinLab 擴充
> [#564](https://github.com/FPI-TW/findb/issues/564) 第 2 階段合併處理。
> 完成後依 `docs/dev/` 的規則，把仍有效的內容整併到 `docs/` 的現行文件並刪除本檔。

## 範圍

需求表的 FMP 工作表共 31 項，另加美股與港股指數日線。需求表上的群組只是規劃分類，實作的單位是端點與
資料形狀；dataset 邊界依資料形狀、更新時機與目標表決定。

已定事項：

- 帳號方案是 **Ultimate**（全球市場含港股、法說會逐字稿、13F、bulk 端點）。
- 指數只要**日線行情**（例如 `^GSPC`、`^NDX`、`^DJI`、`^HSI`），不含成分股名單與異動。
- **港股日線改由 FMP 作為正式來源。** FactSet 港股尚未在任何環境正式跑通，之後港股不再依賴 FactSet。
  FactSet 的其他資料需求不受影響。
- 美債殖利率曲線（`/treasury-rates`）不做，改用 FRED。
- **美股個股日線維持 Twelve Data。**
- **還原價要存**，以調整因子表示，所有 provider 共用，設計見
  [還原價：調整因子表設計](price-adjustment-factors.md)。
- **VWAP 不由 FMP 取得，也不在本次範圍。** 港股改從 HKEX 取得，台股由 FinLab 成交金額計算，之後一起做；
  美股不做。實測與規劃見「VWAP」。
- **港股原始價先以 FMP 上線，加上 DQ；接上 HKEX 每日報表後每天核對，不一致以 HKEX 為準。** FMP 港股有價格精度、
  成交量與公司行動前後的系統性誤差，見「港股與 HKEX 官方值比對」。
- **HKEX 每日報表同步規劃，先做配合 FMP 的部分**（每日核對與修正），列為「階段 2b」；VWAP 仍與台股之後一起做。
  - HKEX 為獨立 provider 與 dataset，以欄位層修正 FMP 港股列，開盤價保留 FMP 的值。
  - FMP 沒有的港股標的第一版只存在 HKEX 的表。
  - 原始報表歸檔到 R2。
- **每日抓取用 `eod-bulk`**，目標日之後有分割生效的標的改以單檔 `non-split-adjusted` 補抓，見「每日抓取方式」。
- 不設原定交付日，目標是盡快上線。

## 需求依資料形狀分類

括號內是需求表的列號。

| 類別 | 項目 | 現有機制能否承接 |
| --- | --- | --- |
| A 交易日行情 | EOD 日線：美股、類股 ETF、全球指數、港股、跨資產（1）；未調整分割日線，商品期貨用（2）；美股與港股指數日線；盤後最後成交（17） | 日線可走 `market_eod`；指數、全球市場與商品要擴充 catalog enum 與 instrument 模型；盤後成交是快照，不可回補 |
| B 公司行動 | 分割與股息，單檔歷史與日期區間行事曆（21–24） | `corporate_action` 表已存在，缺 contract 與 normalizer |
| C 參考資料快照 | 公司檔案（29）、流通股數（30） | 部分可放 `instruments`；股本與流通數需要版本化 |
| D 定期申報（會修正） | 季度三大報表（3–5）、分析師逐期預估（12）、13F 機構持股彙總（16） | 無；需要期間鍵與修訂語意 |
| E 每日覆蓋型快照 | TTM 比率與關鍵指標（6–8）、評等共識、目標價共識與摘要（9–11）、DCF 輸入（13） | 無；來源只給最新值，不可回補，需要逐日留存 |
| F 事件與行事曆 | 財報行事曆與單檔財報日（18–19）、總經行事曆（20）、內部人 Form 4（25）、參議員交易（26） | 無；日期區間端點，需要事件鍵與更新語意 |
| G 文字 | 法說會逐字稿與日期清單（14–15）、個股與市場新聞（27–28） | 無；大文字，宜存 R2 物件加索引 |

## 現有機制的限制

以下已在程式碼中核對（2026-11-19，`main` 為 `bfea4c5`）：

- **Fetcher 只有按交易日排程的工作。** 每個 dataset 每個日曆日一份 plan，休市日記為 `skipped_calendar`；另有
  universe maintenance 與 rerun（`fetcher/src/findb_fetcher/full_market_runtime.py`）。沒有定期申報、事件區間或
  新聞類的工作型態。
- **Catalog enum 很窄。** `backend/app/provider_catalog.py` 的 `FeedDefinition` 只接受 `market` 為 US／HK／TW、
  `asset_class` 為 equity／etf／future、`frequency` 為 daily／minute／tick；非盤中 feed 一律視為 daily。
- **Contract 只有行情。** `market_eod`（v1、v2）、`futures_eod`（`source` 固定為 TAIFEX，品種限 TX／MTX 等）、
  `intraday_partition`。商品期貨不能用 `futures_eod`。
- **`market_data_eod` 只存 as-traded 價格**，沒有調整價與 VWAP 欄位
  （[ingress contracts](../architecture/ingress_contracts.md)）。
- **`corporate_action`、`macro_series`／`macro_observation` 有表但沒有 writer**；財報、估值、預估、新聞、逐字稿、
  持股、行事曆都沒有 canonical 表，Serve 也沒有對應端點。
- **Full-market 完整性綁定交易日。** `backend/app/services/full_market.py` 的 plan、收盤檢查與成員分類都以
  `trade_date` 與行情表為準。非交易日資料要用 `full_market.required=false` 加自己的 freshness 監控，或新增
  完整性分支。
- **每個 dataset 只能有一條 Source feed**（`backend/scripts/seed_data.py` 的 `_catalog_feed`），canonical upsert
  依 dataset 的 `source_precedence` 整列取捨，不做欄位合併。
- **Catalog 沒有每日呼叫上限。** `platform_limits` 只有 `requests_per_minute` 與 `max_response_bytes`，Fetcher 的
  `requests_per_day` 寫死為空值。
- **FMP 的 API key 可以用 `apikey` header 傳送**（已實測），和現有 provider 一樣不放進 URL；raw evidence 與錯誤
  訊息仍須確認不含 key。

## 階段 1 盤點結果

2026-11-19 以正式 key 逐項試打需求表 31 項與指數日線，原始回應保存在不進版控的位置。

### 權限與共通行為

- **全部 HTTP 200，Ultimate 沒有遇到方案限制。** 包含 13F、逐字稿、`eod-bulk`、盤後成交、參議員交易。
- 沒帶 key 回 HTTP 401；權限不足回 HTTP 402（10/21 以舊 key 遇過）。回應 header 沒有剩餘額度資訊。
- **不存在的代號、未來日期、週末都回 HTTP 200 與 `[]`**，無法從回應區分「沒有這檔」「尚未發布」「休市」。

### 日線價格語意

- **`historical-price-eod/full` 是分割調整價。** NVDA 2024-06-07 在 `full` 是 120.89，實際成交是 1208.9。
- **原始成交價要從 `historical-price-eod/non-split-adjusted` 取**；它的欄位叫 `adjOpen` 等，值卻是原始價。
  `dividend-adjusted` 是分割加股息調整。三者的關係與因子驗證見
  [還原價：調整因子表設計](price-adjustment-factors.md)。
- **`vwap` 不是成交量加權均價。** 975 列中 965 列剛好等於 (open + high + low + close) ÷ 4，其餘是四捨五入差或
  未收盤列。FMP 沒有提供真正的 VWAP。
- **`change`／`changePercent` 是當日 close − open**，不是與前一日比較。
- **未收盤的當日也會回一列。** 港股 10:24 查詢時，`0700.HK`、`^HSI`、`^HSCE` 都回了當天的盤中值，且
  `0700.HK` 的成交量與前一日完全相同。抓取必須等收盤發布之後，並只接受目標日期的列。港股的發布時間尚未測量。
- `unadjustedVolume` 不存在。
- 歷史起點：`0700.HK` 從 2004-06-16（上市日），`^GSPC` 從 1927-12-30。
- **`eod-bulk`**：一天一次呼叫，回 CSV。
  - 11/18 為 3.7 MB、65,103 列，涵蓋全球市場，其中港股 2,304 列、指數 381 列。
  - 欄位只有 `symbol,date,open,low,high,close,adjClose,volume`，沒有 vwap。
  - **`open`／`high`／`low`／`close` 與 `volume` 是分割調整值**，等於 `full`；`adjClose` 是分割加股息調整，等於
    `dividend-adjusted`。以 2026-11-06（之後一週內有 8 檔分割，美股與港股都有）與 NVDA 2024-06-07 驗證。
  - 因此 bulk 不能當作歷史原始價；只有目標日之後沒有分割生效的標的，bulk 的值才等於原始價。
  - 分割的比值跳動都落在清單上的分割日，沒有股息那種差一天的情形。

### 港股與 HKEX 官方值比對

2026-09-21 至 11-18 共 41 個交易日，以 HKEX 每日報表（主板代號 1–9999、HKD）為官方值，比對 FMP
`non-split-adjusted`：

| 項目 | 結果 |
| --- | --- |
| HKEX 有成交的列 | 92,138；兩邊都有 85,306 |
| 收盤價完全一致 | 85.2% 的列，占成交金額 90.5% |
| 收盤價差 0.005 以內 | 14.5% 的列，FMP 四捨五入到小數 2 位所致 |
| 收盤價差超過 1% | 239 列、65 檔，占成交金額 0.01%，集中在併股、分割、供股前後 |
| 成交量差 1% 以內 | 82.6% 的列；差超過 1% 的只占成交金額 0.69% |
| HKEX 有成交、FMP 沒有 | 285 檔、6,832 列，占成交金額 1.16%；幾乎都是整檔沒有資料 |
| FMP 有列、HKEX 當天沒成交或停牌 | 13,971 列，其中 13,792 列成交量為 0 |

三個系統性問題：

1. **價格精度不足。** HKEX 有 0.005 的報價單位（例如 1.265），FMP 對 0.5 港元以上的價格多只留 2 位小數。
   0.25 港元以下仍保留 3 位。
2. **冷門股的成交量偏多。** 差超過 1% 的列有 96% 是 FMP 較多，461 列剛好 2 倍。抽查 5 檔，多出的量常等於午休前或
   收盤前最後一筆，推測是跨時段邊界的成交重複計算，尚未證實。HKEX 的成交股數等於當日全部成交紀錄的加總。
3. **有公司行動的標的，「未調整」端點會錯。**
   - 1657.HK 於 10-05 做 10 合 1，併股前 FMP 回 550、HKEX 是 55：回的其實是調整後價格。
   - 0381.HK 於 11-13 做 2 合 1，收盤價正確，但併股前的成交量約為 HKEX 的 2.1–2.2 倍；FMP 只調整了價格、沒調整
     成交量，未調整端點再用同一因子反推。
   - 部分標的（例如 1628.HK、948.HK）期間內沒有登記分割，仍有成倍差異。
   - 美股的反分割（JOB 1:30）與正分割（NVDA）沒有這個問題。

### 發布時間

- **HKEX**：41 份報表的 `Last-Modified` 全部是當天香港時間 20:00，20:00 前回 404。
- **FMP 港股**：收盤前就會回當天的列，值隨盤中變動；何時定案、是否與 HKEX 收盤一致，2026-11-19 實測中
  （每 5 分鐘查一次單檔與 `eod-bulk`，到香港時間 21:30）。
- 美股尚未測量。

### 其他端點

| 項目 | 結果與限制 |
| --- | --- |
| 季度三大報表 | 美股、港股都有。港股 `filingDate` 等於期末日、`acceptedDate` 是 00:00，不是真實申報時間；報表幣別可能與交易幣別不同（0700.HK 為 CNY） |
| TTM 比率與關鍵指標 | 有資料，但**回應沒有日期、期別、幣別**，只能當作抓取時的快照存。需求的 `dividendYieldPercentageTTM` 實際叫 `dividendYieldTTM`；每股淨利與每股淨值在 `ratios-ttm`，不在 `key-metrics-ttm` |
| 評等共識、目標價 | 美股都有；**0700.HK 的目標價共識與摘要都是空的**，評等只有 2 家 |
| 分析師預估、DCF、13F、逐字稿 | 都有。逐字稿欄位是 `year` 不是 `fiscalYear`；港股有逐字稿日期清單 |
| 盤後最後成交 | 毫秒 epoch，只有最後一筆，屬於快照 |
| 財報行事曆 | **單次最多 4,000 筆，超過會截斷且不報錯**，必須逐日查詢並以筆數小於 4,000 確認完整 |
| 總經行事曆、新聞、流通股數 | 時間戳沒有時區，需確認 |
| 分割、股息 | 單檔與行事曆都有；港股股息的日期常與還原價跳點差一個交易日 |
| 內部人、參議員交易 | 都有；需求的 `capitalGainsOver200USD` 不存在 |
| 個股新聞 | **0700.HK 沒有新聞** |
| 清單 | `index-list` 428 筆；`etf-list` 16,896 筆，沒有類股分類，類股 ETF 需要自訂名單；`commodities-list` 40 筆，是連續近月，部分幣別為 `USX`（美分）；港股名單可用 `company-screener?exchange=HKSE`（約 2,606 檔） |

## 分階段

### 階段 1：唯讀盤點（已完成）

結果見「階段 1 盤點結果」。尚未測量的項目併入階段 2 開工前確認：

- 港股與美股 EOD 的發布時間，以及未收盤列在收盤後是否會被修正。
- 30 日頻寬上限與實際用量、429 與 `Retry-After` 行為。
- FMP 缺少的 285 個港股代號屬於哪些類型（例如 ETF、REIT、債券），以及是否在需求範圍內。

### 階段 2：Provider 基礎與第 1 批交易日行情

範圍：A 類的日線，包含港股個股、美股與港股指數、全球指數、類股 ETF、跨資產。盤後最後成交（17）另議。

- 原始價每日取自 `eod-bulk`、歷史回補取自 `non-split-adjusted`，不用 `full`；只接受目標日期且已發布的列。
- 調整因子依 [還原價：調整因子表設計](price-adjustment-factors.md) 另行建立，不阻擋原始價上線。

#### 每日抓取方式

- 每個交易日收盤發布後呼叫一次 `eod-bulk`，取目標日的列。
- 以 `splits-calendar` 找出生效日晚於目標日的分割；這些標的的 bulk 值已被調整，改以單檔 `non-split-adjusted`
  補抓。收盤後立即抓取時通常沒有這種標的，主要發生在重試與補抓。
- 歷史回補一律逐檔呼叫 `non-split-adjusted`。

#### 港股 DQ

依「港股與 HKEX 官方值比對」的結果：

- 目標日前後的窗口內有分割、併股或供股的標的，原始價可能錯誤：該窗口內的列標記 warning，並以事件前後的比值
  與事件比例核對，比值不符時為 error 不寫入。窗口長度在實作時定。
- 價格精度只到小數 2 位、冷門股成交量偏多，寫進 dataset 文件的已知限制，不在 DQ 擋下。
- FMP 在無成交或停牌日仍回成交量為 0 的列，寫入前須與現行 `market_eod` 對無成交日的處理一致。
- 接上 HKEX 後，每天以 HKEX 報表核對並修正收盤價、高低價與成交量，見「階段 2b」。

- **Backend**
  - `backend/configs/provider_catalog.v1.json` 新增 provider `fmp` 與 feeds；擴充 `FeedDefinition` enum（指數、
    全球市場）；以 `backend/scripts/export_provider_catalog.py` 重產生 artifacts。
  - `backend/app/config.py` 新增 `FMP_SKIP_NON_TRADING_DAYS`（`effective_calendar` 依 `calendar_switch_env` 讀取）。
  - `backend/scripts/seed_data.py` 的 `SUPPORTED_DATASETS` 與 dataset 定義。
  - Alembic migration 建立 registry、scheduler dataset、control `full_market_fmp_v1` 與 Source credential scope，
    做法參考 `f8b0d2c4e6a9_stage_finlab_intraday_feeds.py`。
  - `seed_datasets` 會執行 `e9f4a1c6b3d5` 凍結的 inspection，它不認得新 control，會判為 `unknown_difference`；
    須一併處理。
- **Fetcher**
  - `providers/fmp.py`：client、`from_env`、contract mapping、key 遮蔽。
  - `provider_adapters/fmp.py`：`ProviderAdapter` 子類，登記到 `ADAPTERS`。
  - 共用程式碼裡的 provider 清單：`execution_config._IMPLEMENTED_ADAPTERS`、`historical_backfill_cli.py` 的
    runner、`acquisition.py` 的 scheduler key、`scheduler_control._SCHEDULER_KEYS`、`provider_work.py` 的 replay
    decoder、`production_backfill_cli._scope`。
  - 沿用 `image_key: twelve_data`（同 TAIFEX），不新增 image、ECR 與 workflow。
- **Infra**：`release_fetcher_provider.sh`、`deploy_fetcher_aws.sh`、`fetcher.json` secrets bundle、
  `infra/tofu/{staging,production}/secrets.tf` 與 `monitoring.tf`、`fetcher-ci.yml` 的 provider 迴圈。
- **港股來源切換**
  - `hk_equity_eod` 的 Source feed 由 `factset` 改為 `fmp`。
  - Staging 目前的港股來源是 FactSet；`backend/app/services/hk_provider_cutover.py` 把 twelve_data → factset
    寫死，需要一般化或另寫切換流程。Production 的現行港股來源須先核對。
  - FactSet 港股 feed 比照 Shioaji 退役：controls retired、credential scope 收回，既有 canonical、raw、plans 與
    receipts 保留。
  - [backlog](backlog.md) 的「FactSet 港股首次環境啟用」不再推進。
- **測試與驗收**：Fetcher per-provider 測試與共用矩陣（`test_provider_adapters.py`、golden fixtures、
  `execution_config.v1.json`）、`backend/tests/test_provider_catalog.py`、infra inventory 測試；
  Source → raw → queue → canonical → Serve 驗收；staging 只啟動 `fmp`，首日先設成單日再 start。

### 階段 2b：HKEX 每日報表（配合 FMP 港股）

範圍只到「每日核對與修正 FMP 港股原始價」；VWAP 的計算與 Serve 欄位仍列在 [backlog](backlog.md#vwap)。
成交金額在這一階段就一併存下，之後 VWAP 不必重抓。

#### 報表事實

- 網址形如 `https://www.hkex.com.hk/eng/stat/smstat/dayquot/d<yymmdd>e.htm`，公開、不需帳號。
  - 每個交易日一個檔，約 22–24 MB，gzip 後約 3.5 MB。
  - 涵蓋全部港股：前收、最高、最低、收盤、買賣價、成交股數、成交金額；停牌以狀態文字標示
    （11-18 有 362 檔為 `TRADING SUSPENDED`）。
  - 11-18 共 16,914 列，其中 13,408 列是代號 10000 以上的權證與牛熊證；代號 1–9999 約 3,500 列。
  - **沒有開盤價。**
- **每個交易日香港時間 20:00 產生。** 2026-09-21 至 11-18 共 41 份報表的 `Last-Modified` 全部落在當天
  20:00；週末與當天 20:00 前回 404，假日回內容為「NO TRADING」的頁面。
- **只保留約兩個月**：2026-09-21 以後可取，更早的回 404。上線後只能往回補到當時仍保留的日期。
- 連續 HEAD 請求曾一律回 503；GET 與 `Range` 請求正常。
- 解析：QUOTATIONS 段落，每檔兩行；幣別除 HKD 外還有 USD、CNY 等櫃台，納入範圍跟港股名單一致。

#### 與 FMP 的發布時間比較

見「發布時間」。

#### 合併方式

Owner 於 2026-11-19 確認以下做法。

`market_data_eod` 一列只來自一個來源、依優先序整列取捨；HKEX 沒有開盤價，不能整列取代 FMP 的列，而且
「每個 dataset 只能有一條 Source feed」。因此：

- HKEX 是獨立的 provider `hkex` 與 dataset（暫稱 `hk_equity_official_eod`），寫入自己的 canonical 表，以
  `(instrument_id, trade_date)` 為鍵，存報表的全部數值欄位與交易狀態。
- **欄位層修正**：同一交易日兩邊都有資料時，Backend 以 HKEX 的收盤、最高、最低、成交量、成交金額覆寫
  `market_data_eod` 的港股列，開盤價保留 FMP 的值。
  - 不論誰先寫入結果都相同：HKEX 寫入時修正既有的 FMP 列；FMP 重抓或補抓時，若同日已有 HKEX 值，寫入後
    再套用一次。
  - 被取代的 FMP 原值與差異記錄下來（`dq_issue` 或專用稽核表），列上註明哪些欄位來自 HKEX。具體欄位待定。
  - 差異超過門檻（例如收盤價不同且不是 0.005 的四捨五入、成交量差超過 1%）另開 warning，供監控 FMP 品質。
- **HKEX 有、FMP 沒有的標的**（目前 285 檔、占成交金額 1.16%）：第一版只存在 HKEX 的表，不在
  `market_data_eod` 新增開盤價為空的列。
- **HKEX 停牌、FMP 卻回成交量 0 的列**：以 HKEX 的狀態判定為無成交，處理方式與「港股 DQ」一致。

#### Backend

- Contract 可沿用 `market_eod` v2：`open` 可為空，已有 `turnover`；交易狀態等額外欄位若需要，改用新版本或
  新 contract。
- 新 canonical 表、normalizer、上述欄位層修正、DQ。DQ 至少包括：
  - 成交金額 ÷ 成交股數必須落在當日最低與最高之間（同時驗證解析是否正確）。
  - 收盤價落在最低與最高之間；成交股數為 0 時不得有成交金額。
- Catalog 新增 provider `hkex` 與 feed；`calendar_market` 為 HK。
  - 排程：香港時間 20:05 起，重試到當晚；完成期限沿用港股日線的隔天 06:30。
  - 假日頁以「NO TRADING」作為 no_data 的 durable evidence。
- Registry、control、Source credential 與 migration 的做法同階段 2。

#### Fetcher

- 比照 TAIFEX：官方報表、沒有 credential、沿用 `image_key: twelve_data`。
- 回應約 24 MB，超過現行 provider 的 `max_response_bytes`（16 MB），`hkex` 需設較高的上限。
- Raw 層不存 24 MB 的 HTML：contract 只帶名單範圍內解析後的列，`source_raw_sha256` 記原始報表雜湊。
- **原始報表歸檔到 R2**（Owner 於 2026-11-19 確認）：HKEX 兩個月後就刪除，歸檔後才能事後重新解析或擴大範圍
  （例如權證與牛熊證）。
  - 每天一份 gzip，約 3.5 MB，一年約 1 GB。
  - 長期保存，不套用 raw 的 30 天 TTL；contract 以 `source_raw_ref` 指向歸檔物件，雜湊與 `source_raw_sha256` 一致。
  - bucket、路徑與寫入時機（先歸檔再送 contract）在實作時定，參考盤中資料的 R2 做法
    （[intraday storage](../architecture/intraday_storage.md)）。
- 只用 GET，控制請求頻率；20:00 前的 404 視為尚未發布。

#### 歷史

- 上線時能補到的最早日期是「上線日往前約兩個月」，更早的港股歷史維持 FMP 的值。
- 本機已有 2026-09-21 至 11-18 的 41 份報表，可用於開發與驗收的 fixture，但不經 Source 寫入正式環境。
- 每晚一天上線，就少一天可修正的官方資料；建議與階段 2 同批上線。

### 階段 3：公司行動

- 新增 `corporate_action` contract 與 normalizer、DQ 與 Serve 端點，涵蓋分割與股息。
- 同一套 contract 也承接 backlog 的「匯入 Twelve Data splits／dividends」。
- 調整因子以 `corporate_action_id` 連到事件；因子本身從 provider 還原價推導，見
  [還原價：調整因子表設計](price-adjustment-factors.md)。

### 階段 4：非交易日資料的共用設計

與 #564 第 2 階段合併，依發布型態分別定義：

- 工作與完整性模型：定期申報（D）、覆蓋型快照（C、E）、事件（F）、文字（G）。
- 版本化儲存：first-seen 時間、修訂、來源 as-of。
- Contract、canonical 表、Serve 形狀與 freshness 監控。

每一類分別核准後才進入實作。

### 階段 5 之後：分批導入 C–G 類

批次順序待決。建議先處理不可回補的 E 類與盤後最後成交，必要時先只留存 raw；raw 保留期限需要另訂規則。

## VWAP

Owner 於 2026-11-19 決定：

- 港股 VWAP 改從 HKEX 取得。
- 台股 VWAP 由 FinLab 的成交金額計算。
- 兩者都屬於 FMP 以外的 provider 工作，不在本次範圍，之後一起做；追蹤見 [backlog](backlog.md#vwap)。
- 美股不做。
- FMP 的 `vwap` 欄位不存。

### 實測（2026-11-18 交易日）

以 HKEX 每日報表的成交金額 ÷ 成交股數為官方值：

| 標的 | 官方 VWAP | FMP 的 `vwap`（OHLC 平均） | 由 FMP 1 分 K 計算 | 1 分 K 成交量占全日 |
| --- | ---: | ---: | ---: | ---: |
| 0700.HK | 372.946 | +0.20% | −0.016% | 83% |
| 0005.HK | 135.959 | +0.45% | −0.005% | 89% |
| 0131.HK | 1.118 | +0.61% | +0.16% | 51% |

- **FMP 的 1 分 K 不適合作為 VWAP 來源。**
  - 港股只到 15:59，沒有 16:00–16:10 的收盤競價。
  - 冷門股的分 K 不完整，0131.HK 當天只有 5 根，其中 3 根成交量為 0。
  - 2021 年沒有資料。
- **FMP 與 HKEX 的日成交量不完全相同**：0700.HK 差 4,900 股，0131.HK 差 1,000 股。
  VWAP 必須用同一來源的成交金額與成交股數計算，不能拿 HKEX 的金額除以 FMP 的股數。

### 港股

- 來源是 HKEX 每日報表，報表事實與取得方式見「階段 2b」；成交金額與成交股數在階段 2b 就會存下。

### 台股

- FinLab 的 `price:成交金額`（免費，2007-04-23 起）和現有使用的 `price:成交股數` 同一來源，相除即為精確 VWAP。

### 儲存（之後實作時再定案）

若階段 2b 採欄位層修正，港股的成交金額與成交量會同時來自 HKEX，`market_data_eod` 的 `turnover ÷ volume`
即為 VWAP，可能不需要另建表；台股同理可把 FinLab 的成交金額寫入 `turnover`。
否則另建一張以 `(instrument_id, trade_date)` 為鍵的小表，欄位包括：

- `vwap`
- `method`：例如 `turnover`
- 計算所用、且來自同一來源的成交金額與成交股數
- 現有的來源欄位慣例

## 待決事項

- 美股 515 檔的名單來源，以及每個 dataset 的 bounded universe。港股名單沿用現有的 HKEX `ListOfSecurities`
  （`fetcher/src/findb_fetcher/full_market_universe.py`），代號轉成 FMP 的 `XXXX.HK`。
- 階段 2b：欄位層修正的來源紀錄欄位、歸檔的 bucket 與路徑。
- 指數、商品與跨資產的 instrument 模型：市場代碼、交易日曆、幣別。
- 階段 5 的批次順序。
- 授權條款是否允許內部再提供給下游專案。
