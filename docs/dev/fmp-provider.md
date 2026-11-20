# FMP（Financial Modeling Prep）provider 導入規劃

> 狀態：階段 1 盤點與港股 HKEX 比對已完成（2026-11-19）；已定事項由 Owner 確認，其餘列在「待決事項」，尚未開始實作。
> 2026-11-20 起港股個股不由 FMP 供應，下一個優先是市場指數。追蹤 issue：
> [#606](https://github.com/FPI-TW/findb/issues/606)。非交易日資料的共用設計與 FinLab 擴充
> [#564](https://github.com/FPI-TW/findb/issues/564) 第 2 階段合併處理。
> 完成後依 `docs/dev/` 的規則，把仍有效的內容整併到 `docs/` 的現行文件並刪除本檔。

## 範圍

需求表的 FMP 工作表共 31 項，另加美股與港股指數日線。需求表上的群組只是規劃分類，實作的單位是端點與
資料形狀；dataset 邊界依資料形狀、更新時機與目標表決定。

已定事項：

- 帳號方案是 **Ultimate**（全球市場含港股、法說會逐字稿、13F、bulk 端點）。
- 指數只要**日線行情**（例如 `^GSPC`、`^NDX`、`^DJI`、`^HSI`），不含成分股名單與異動。
- **FMP 不承接港股個股日線**（Owner 於 2026-11-20 決定）。2026-11-19 曾決定港股改由 FMP 供應，三方比對後改回
  FactSet，並以 HKEX 官方值優先，見 [港股個股日線：FactSet 供應、HKEX 官方值優先](hk-equity-factset-hkex.md)。
- **下一個優先是市場指數日線**（美股與港股），見「階段 2」。
- 美債殖利率曲線（`/treasury-rates`）不做，改用 FRED。
- **美股個股日線維持 Twelve Data。**
- **還原價要存**，以調整因子表示，所有 provider 共用，設計見
  [還原價：調整因子表設計](price-adjustment-factors.md)。
- **VWAP 不由 FMP 取得，也不在本次範圍。** 港股改從 HKEX 取得，台股由 FinLab 成交金額計算，之後一起做；
  美股不做。實測與規劃見「VWAP」。
- **港股指數排在香港時間 20:00 抓取**（Owner 於 2026-11-19 確認）。
- **每日抓取用 `eod-bulk`**，目標日之後有分割生效的標的改以單檔 `non-split-adjusted` 補抓，見「每日抓取方式」。
- 不設原定交付日，目標是盡快上線。

## 需求依資料形狀分類

括號內是需求表的列號。

| 類別 | 項目 | 現有機制能否承接 |
| --- | --- | --- |
| A 交易日行情 | EOD 日線：美股、類股 ETF、全球指數、跨資產（1；港股個股改由 FactSet）；未調整分割日線，商品期貨用（2）；美股與港股指數日線；盤後最後成交（17） | 日線可走 `market_eod`；指數、全球市場與商品要擴充 catalog enum 與 instrument 模型；盤後成交是快照，不可回補 |
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

> 這是決定 FMP 不承接港股個股的依據之一；與 FactSet 的三方比對見
> [港股個股日線：FactSet 供應、HKEX 官方值優先](hk-equity-factset-hkex.md#三方比對)。

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

另一個現象：**FMP 成交量為 0、HKEX 當天有成交**的列，主板有 462 列（約 0.5%）。

#### GEM

同期間以 GEM 每日報表（320 個代號，全部 HKD）比對同一端點。GEM 41 天成交金額約 31.6 億港元，約為主板的 0.04%，
但占港股名單 306 檔（約 11%）。

| 項目 | 結果 |
| --- | --- |
| HKEX 有成交的列 | 7,835；兩邊都有 7,685 |
| 收盤價完全一致 | 94.8% 的列，占成交金額 84.2% |
| 收盤價差 0.005 以內 | 325 列，其中 301 列是 2 位小數的四捨五入；占成交金額 14.7% |
| 收盤價差超過 1% | 58 列、27 檔，占成交金額 0.49% |
| 成交量差超過 1% | 23.9% 的列，**占成交金額 12.7%**（主板 0.69%） |
| FMP 成交量為 0、HKEX 有成交 | 118 列（約 1.5%） |
| HKEX 有成交、FMP 沒有 | 22 檔、150 列，占 GEM 成交金額 1.65% |

- 主板的三個問題在 GEM 都出現，成交量偏多更明顯：差超過 1% 的 1,838 列中 1,720 列是 FMP 較多。
- **收盤價有 4 檔整段錯成倍數，分割行事曆都沒有登記**：8123 在 09-21 至 10-13 是 HKEX 的 20 倍，8221 在
  09-21 至 10-12 是 50 倍，8619 在 10-22 至 11-12 是 10 倍，8367 在 09-23 至 10-06 是 1/8。27 檔中只有 2 檔有登記分割。
- 整檔缺資料的 14 個代號：一般股票 9 檔（占 GEM 成交金額 1.64%，最大是 8585、8090）、併股期間的平行櫃台 2 檔
  （8552、8557）、供股權 2 檔、權證 1 檔。
- 結論：FMP 單獨供應 GEM 的品質不足。

### 發布時間

2026-11-19 實測（香港時間）：FMP 每 5 分鐘查一次 7 檔單檔、2 個指數與 `eod-bulk`，到 21:30；HKEX 從 19:50 起每分鐘
以 `Range` 請求查一次主板報表。

- **HKEX** 報表的發布時間見 [港股個股日線](hk-equity-factset-hkex.md#發布時間)。
- **FMP 港股**：收盤前就回當天的列，回應不標示是否定案。
  - `eod-bulk` 的港股列數到 16:32 停在 2,271；數值的大量變動到 16:53 結束（16:32 一輪 833 檔、16:53 一輪 180 檔），
    之後到 19:30 每輪仍有 1–15 檔的收盤價在改，**19:30 以後到 21:30 沒有再變**。
  - 單檔：0700.HK 收盤價 16:11 為 346.8、16:17 為 347.4、16:37 為 348.4；0131.HK 17:56 由 1.11 改為 1.08。
  - 指數：^HSI、^HSCE 在 17:40 定案。
- **FMP 定案值與 HKEX 比對**（主板 HKD 有成交 2,248 檔，`eod-bulk` 有 2,059 檔；港股個股已不由 FMP 供應，保留作為紀錄）：
  - 收盤價依成交金額 89.5% 完全一致，10.5% 是 2 位小數的四捨五入，其他差異 14 檔。
  - **當天的成交量大型股偏少**：86 檔少超過 1%，占成交金額 17%，例如 0005.HK 少 8%、0388.HK 少 10%、3988.HK
    少 12%；這些值 17:00 後沒有再變。歷史比對中大型股多半一致，推測 FMP 之後才補上部分成交，待隔天重抓 11-19
    確認。冷門股偏多的情形同歷史比對（333 檔，占成交金額 0.09%）。
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

- 美股指數的發布時間，以及未收盤列在收盤後是否會被修正（港股指數 11-19 在 17:40 定案）。
- 30 日頻寬上限與實際用量、429 與 `Retry-After` 行為。

### 階段 2：Provider 基礎與市場指數日線

範圍：美股與港股的市場指數日線（例如 `^GSPC`、`^NDX`、`^DJI`、`^HSI`、`^HSCE`），Owner 於 2026-11-20 定為下一個
優先。全球指數、類股 ETF、跨資產與盤後最後成交（17）在之後的批次。

- 指數沒有分割，`eod-bulk` 的值就是原始值；11-18 的 bulk 有 381 列指數。指數的成交量是 0。
- 只接受目標日期且已發布的列：FMP 收盤前就回當天的列，回應也不標示是否定案。
- 港股指數只有 FMP 一個來源，沒有官方值可核對；11-19 在 17:40 定案，排在香港時間 20:00 抓取。
- 美股指數的發布時間尚未測量，排程依實測決定。

#### 每日抓取方式

- 每個交易日收盤發布後呼叫一次 `eod-bulk`，取目標日的指數列；一次呼叫涵蓋全部指數。
- 歷史回補逐檔呼叫 `historical-price-eod/full`（指數沒有分割，與 `non-split-adjusted` 相同），`^GSPC` 從 1927 年起。
- 個股與 ETF 之後若改由 bulk 取得，目標日之後有分割生效的標的要改以單檔 `non-split-adjusted` 補抓。

#### 實作要點

- **Backend**
  - `backend/configs/provider_catalog.v1.json` 新增 provider `fmp` 與指數 feeds；擴充 `FeedDefinition` 的
    `asset_class`（`index`），以 `backend/scripts/export_provider_catalog.py` 重產生 artifacts。
  - Instrument 模型：指數的代號、市場、交易日曆與幣別，見「待決事項」。
  - `backend/app/config.py` 新增 `FMP_SKIP_NON_TRADING_DAYS`（`effective_calendar` 依 `calendar_switch_env` 讀取）。
  - `backend/scripts/seed_data.py` 的 `SUPPORTED_DATASETS` 與 dataset 定義；registry、scheduler dataset、control
    `full_market_fmp_v1` 與 Source credential scope。
  - 完成期限固定為排程加 12 小時（`backend/app/provider_catalog.py` 的
    `COMPLETION_DEADLINE_HOURS_AFTER_SCHEDULE`），港股指數 20:00 開始即為隔天 08:00。
  - 新增 control 與 feed 時，要一併提供新的凍結 schedule inspection（目前是 `e9f4a1c6b3d5`，由
    `scripts/preflight_catalog_schedule.py` 在每次部署前執行），否則會判為 `unknown_difference`、中止部署。
- **Fetcher**
  - `providers/fmp.py`：client、`from_env`、contract mapping；key 只放在 `apikey` header，錯誤訊息遮蔽。
  - `provider_adapters/fmp.py`：`ProviderAdapter` 子類，登記到 `ADAPTERS`；一份 bulk 供當天所有成員共用一個
    raw 物件。bulk 是 CSV，需要新的 raw 格式（例如 `csv_utf8`）。
  - 共用程式碼裡的 provider 清單：`execution_config._IMPLEMENTED_ADAPTERS`、`historical_backfill_cli.py` 的
    runner、`production_backfill_cli._scope`。
  - 沿用 `image_key: twelve_data`（同 TAIFEX），不新增 image、ECR 與 workflow。
- **Infra**：`release_fetcher_provider.sh`、`deploy_fetcher_aws.sh`、`fetcher.json` secrets bundle、
  `infra/tofu/{staging,production}/secrets.tf`、`deployment_transaction.py`、`fetcher-ci.yml` 的 provider 迴圈。
- **測試與驗收**：Fetcher per-provider 測試與共用矩陣（`test_provider_adapters.py`、golden fixtures、
  `execution_config.v1.json`）、`backend/tests/test_provider_catalog.py`、infra inventory 測試；
  Source → raw → queue → canonical → Serve 驗收；staging 只啟動 `fmp`，首日先設成單日再 start。

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

- 來源是 HKEX 每日報表，報表事實與取得方式見 [港股個股日線](hk-equity-factset-hkex.md#hkex-每日報表)；成交金額與成交股數在
  HKEX 接入時就會存下。

### 台股

- FinLab 的 `price:成交金額`（免費，2007-04-23 起）和現有使用的 `price:成交股數` 同一來源，相除即為精確 VWAP。

### 儲存（之後實作時再定案）

港股採欄位層修正，成交金額與成交量會同時來自 HKEX，`market_data_eod` 的 `turnover ÷ volume`
即為 VWAP，可能不需要另建表；台股同理可把 FinLab 的成交金額寫入 `turnover`。
否則另建一張以 `(instrument_id, trade_date)` 為鍵的小表，欄位包括：

- `vwap`
- `method`：例如 `turnover`
- 計算所用、且來自同一來源的成交金額與成交股數
- 現有的來源欄位慣例

## 待決事項

- 第一批指數的清單（美股、港股各哪些）。
- 指數、商品與跨資產的 instrument 模型：`asset_class`、市場代碼、交易日曆、幣別與代號格式（例如 `^HSI`）。
- 美股指數的排程。
- 美股 515 檔的名單來源，以及每個 dataset 的 bounded universe。
- 階段 5 的批次順序。
- 授權條款是否允許內部再提供給下游專案。
