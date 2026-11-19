# FMP（Financial Modeling Prep）provider 導入規劃

> 狀態：階段 1 盤點已完成（2026-11-19）；已定事項由 Owner 確認，其餘列在「待決事項」，尚未開始實作。追蹤 issue：
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
- **還原價與 VWAP 都要存。** 還原價以調整因子表示，所有 provider 共用，設計見
  [還原價：調整因子表設計](price-adjustment-factors.md)；VWAP 的格式見「待決事項」。
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
  - 抽查值與單檔查詢一致；`close` 是否為分割調整價尚未驗證。

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
- `eod-bulk` 的 `close` 是否為分割調整價（決定 bulk 能否取代逐檔查詢）。
- 30 日頻寬上限與實際用量、429 與 `Retry-After` 行為。

### 階段 2：Provider 基礎與第 1 批交易日行情

範圍：A 類的日線，包含港股個股、美股與港股指數、全球指數、類股 ETF、跨資產。盤後最後成交（17）另議。

- 原始價取自 `non-split-adjusted`，不用 `full`；只接受目標日期且已發布的列。
- 調整因子依 [還原價：調整因子表設計](price-adjustment-factors.md) 另行建立，不阻擋原始價上線。

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

## 待決事項

- VWAP 的格式。FMP 沒有真正的 VWAP，可選擇：由 1 分 K 計算、照存 FMP 的 OHLC 平均但改名、或不存。
- 美股 515 檔與港股名單的來源，以及每個 dataset 的 bounded universe。
- 指數、商品與跨資產的 instrument 模型：市場代碼、交易日曆、幣別。
- 階段 5 的批次順序。
- 授權條款是否允許內部再提供給下游專案。
