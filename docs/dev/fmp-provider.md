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
- **FMP 是市場指數的資料來源**（Owner 於 2026-11-20 確認），依據見「指數準確率」。
- 第一批是已比對的 10 檔指數；依更新時間分組、每組一個 dataset；所有指數 feed 都必須有已發布的交易所日曆；VIX 依美股交易日曆、非交易日
  沒有值；控制台依新功能調整。細節見「階段 2」。
- 美債殖利率曲線（`/treasury-rates`）不做，改用 FRED。
- **美股個股日線維持 Twelve Data。**
- **還原價要存**，以調整因子表示，所有 provider 共用，設計見
  [還原價：調整因子表設計](price-adjustment-factors.md)。
- **VWAP 不由 FMP 取得，也不在本次範圍。** 港股改從 HKEX 取得，台股由 FinLab 成交金額計算，之後一起做；
  美股不做。實測與規劃見「VWAP」。
- **指數分兩組**（Owner 於 2026-11-21 定案）：港股組排在香港時間 20:00，美股組排在台北時間隔天 08:30。
  見「分組與排程」。
- **指數每日逐檔查詢**（Owner 於 2026-11-21 決定），不用 `eod-bulk`，見「每日抓取方式」。
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
    少 12%；這些值 17:00 後沒有再變。冷門股偏多的情形同歷史比對（333 檔，占成交金額 0.09%）。
  - **隔天才補正**：11-19 的 `eod-bulk` 在當晚 22:31 與隔天 09:00 幾乎相同（只有 1 檔變動），隔天 20:31 重抓時
    152 檔的成交量或價格改了，另多出 221 列（204 列成交量為 0）。補正後成交量差超過 1% 的部分從占成交金額 17.1%
    降到 0.1%，0005.HK、3988.HK 與 HKEX 完全相同。補正時間落在隔天 09:00 到 20:31 之間，沒有更細的觀測。
    當天 21:30 前的值不是定案值，這也是歷史比對中大型股多半一致的原因。
- 美股尚未測量。

### 指數準確率

2026-11-20 以 2021-01-01 至 2026-11-19 的 10 檔指數（`^GSPC`、`^NDX`、`^DJI`、`^IXIC`、`^RUT`、`^VIX`、`^SOX`、
`^NYA`、`^HSI`、`^HSCE`）比對 yfinance，並以官方值判定差異。`full` 與 `non-split-adjusted` 對指數完全相同。

| 比對 | 結果 |
| --- | --- |
| 與 yfinance：開高低收全部相同的日期 | 97.0%（`^RUT`）至 100%（`^SOX`）；收盤價幾乎全部相同，每檔最多 4 天不同 |
| 與 Cboe 官方 VIX（約 6 年） | 收盤 1,499／1,500 天相同；yfinance 1,476／1,479，另少 31 天 |
| 與 HKEX 每日報表的恒指、國企收市（09-21 至 11-19，42 天） | 兩檔都 42／42 相同 |

- 爭議日期上 FMP 比 yfinance 準，例如 VIX 2026-02-06 官方是 17.76，FMP 相同，yfinance 是 20.37。
- 開盤、高低價有少量差異（`^DJI`、`^NYA` 開盤各約 30 天，`^RUT` 高低價各 21 天），沒有官方值可判定。指數的開盤
  定義各家不一。
- **FMP 在交易所休市日也回資料**，必須以交易所日曆濾掉：
  - 港股休市日的值是假的：`^HSI` 7 天、`^HSCE` 21 天。例如 2024-10-01（國慶休市）FMP 給出 21289.60 → 22371.62 的
    完整 OHLC；颱風停市日則重複前一天的收盤。
  - VIX 在美國假日有值（Cboe 延長交易時段的計算值），`^RUT` 也有一列落在週六。
- FMP 的港股指數只有 `^HSI`、`^HSCE`，**沒有恒生科技指數**。
- 指數成交量各家定義不一（`^NDX` 兩邊幾乎全不同，港股指數 FMP 有 38–46 天是 0），不入庫。
- 美股其他指數沒有取得官方值：FRED 從測試環境連線逾時，Stooq 要求瀏覽器驗證。

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

- ~~美股指數的發布時間，以及未收盤列在收盤後是否會被修正~~：2026-11-20 實測完成，見「階段 2」的「發布時間實測」。
- 30 日頻寬上限與實際用量、429 與 `Retry-After` 行為。

### 階段 2：Provider 基礎與市場指數日線

範圍：美股與港股的市場指數日線，Owner 於 2026-11-20 定為下一個優先。全球指數、類股 ETF、跨資產與盤後最後成交
（17）在之後的批次。

**第一批是已完成準確率比對的 10 檔**（Owner 於 2026-11-20 確認）：

| 市場 | FMP 代號 | 指數 |
| --- | --- | --- |
| 美股 | `^GSPC` | S&P 500 |
| 美股 | `^NDX` | NASDAQ 100 |
| 美股 | `^DJI` | 道瓊工業平均指數 |
| 美股 | `^IXIC` | NASDAQ 綜合指數 |
| 美股 | `^RUT` | Russell 2000 |
| 美股 | `^VIX` | Cboe 波動率指數 |
| 美股 | `^SOX` | 費城半導體指數 |
| 美股 | `^NYA` | NYSE 綜合指數 |
| 港股 | `^HSI` | 恒生指數 |
| 港股 | `^HSCE` | 恒生中國企業指數 |

FMP 的港股指數另外只有 `512.HK`（CES China HK Mainland Index）有資料，不在第一批；恒生科技、紅籌、綜合等恒生指數
都查不到資料，`^XLHK` 在清單上但沒有資料。

Owner 於 2026-11-20 決定：

- **依更新時間分組，每組一個 dataset。** 一個 dataset 每天只有一份工作，沒有「每檔各自的可抓時間」；更新時間不同
  的指數放在同一個 dataset，晚到的那檔會讓整份工作等待或失敗。分組依「發布時間」的實測決定，每組排在組內最晚
  定案的時間之後；2026-11-21 定案，見「分組與排程」。
- **所有指數 feed 都必須有已發布的交易所日曆才執行。** 缺少日曆時不得以週一至週五的預設代替，而是停在可重試的
  等待狀態並顯示原因。FMP 在休市日回的列一律不寫入。
- **VIX 依美股交易日曆，非交易日沒有值。** VIX 由 S&P 500 選擇權計算；Cboe 在美國假日公布的延長交易時段計算值
  不入庫。
- **控制台依新功能調整**：一個 provider 底下有不同市場、時區與排程的 feed，必要時重構介面。

其他已知事實：

- 指數沒有分割，`full` 與 `non-split-adjusted` 的值相同，就是原始值。
- 只接受目標日期且已發布的列：FMP 收盤前就回當天的列，回應也不標示是否定案。
- 港股指數只有 FMP 一個來源，沒有每日官方值可自動核對。

#### 發布時間實測

2026-11-19 21:23 至 11-20 23:00（紐約時間）每 5 分鐘以 `historical-price-eod/full` 查 10 檔，每 30 分鐘查一次
`eod-bulk`。只觀測了一個美股交易日（11-20，星期五）。

| 指數 | 最後一次變動 | 定案收盤 | 核對 |
| --- | --- | ---: | --- |
| `^HSCE` | 16:15 HKT | 7554.37 | HKEX 報表相同 |
| `^HSI` | 17:38 HKT（11-19 為 17:40） | 22410.80 | HKEX 報表相同 |
| `^DJI` | 16:10 ET | 48147.33 | yfinance 相同 |
| `^VIX` | 16:31 ET | 35.89 | Cboe OHLC 全部相同 |
| `^RUT` | 18:20 ET | 2581.79 | yfinance 相同 |
| `^GSPC`、`^IXIC`、`^SOX`、`^NYA` | 18:25 ET | 6910.39 等 | yfinance 相同 |
| `^NDX` | 19:01 ET | 25028.98 | yfinance 相同 |

- 盤中就有當天的列，每 5–10 分鐘更新。
- 美股 18:20–19:01 的變動是真的改收盤，不只是捨入：`^GSPC` 16:00 為 6901.86，18:25 改為 6910.39。之後到
  23:00 ET 沒有再變。
- **前一天的列在半夜被換掉。** 美股 11-19 的列在 11-20 00:03–04:17 ET 之間兩個版本來回切換，之後穩定在正確版本。
  `^NYA` 錯的版本開盤 = 最高 = 前一天收盤、成交量 0；`^VIX` 開盤 = 最低 = 前一天收盤，Cboe 官方不是。
- 港股 11-19 的 `^HSI` 隔天 11:20 HKT 被改：收盤 22841.62 → 22841.63、成交量從 0 補上；HKEX 官方是 22841.62。
- `eod-bulk` 緊接著的第二次呼叫一律 429（70 次全部），單檔端點 2,980 次全部 200。

#### 分組與排程

Owner 於 2026-11-21 定案：

| 組 | 指數 | 排程 | 完成期限 | 交易日曆 |
| --- | --- | --- | --- | --- |
| 港股（dataset `hk_index_eod`） | `^HSI`、`^HSCE` | 香港時間 20:00，`target_date_lag_days` 0 | 隔天 08:00 HKT | HK |
| 美股（dataset `us_index_eod`） | 其餘 8 檔，含 `^VIX` | 台北時間 08:30，`target_date_lag_days` 1 | 同日 20:30（台北） | US |

- dataset 名稱 `hk_index_eod`、`us_index_eod` 由 Owner 於 2026-11-23 確認。
- 美股組以台北時間排程，抓的是前一個紐約交易日。台北 08:30 在美國冬令時間是 19:30 ET，夏令時間是 20:30 ET。
  - 11-20（冬令時間）最晚的 `^NDX` 在 19:01 ET 定案，離排程約 30 分鐘。
  - 前一天的列從 00:03 ET 起被換成錯的版本，台北 08:30 在這之前。
- 星期六 08:30 抓星期五，星期一 08:30 的目標日是星期日，依美股日曆沒有工作。
- 完成期限依現行規則為排程加 12 小時。
- 定案後不再重抓覆寫：觀測到的隔天改值，一種是半夜出現錯的版本，另一種是改成和官方差 0.01。

#### 指數的資料模型

Owner 於 2026-11-23 決定：

- `asset_class` 為 `index`。Canonical 的詞彙表（`backend/app/vocabulary.py`）已有 `index`，只需擴充 catalog
  `FeedDefinition` 的 `asset_class`。
- 市場與交易日曆：美股組 `US`，港股組 `HK`；幣別 `USD`、`HKD`。
- **代號沿用 FMP 原樣**，例如 `^GSPC`、`^HSI`。
- 2026-11-23 唯讀查 production：沒有 `asset_class = 'index'` 的 instrument，也沒有指數 dataset，不會和舊資料衝突。
  `backend/scripts/backfill_world_names.py` 的 `INDEX_NAMES` 第一批 10 檔已改用 FMP 代號（PR 2，新增 `^NYA`）；
  其他指數維持原代號，等之後導入時再對應。

#### 歷史回補

Owner 於 2026-11-23 決定：**回補功能本次要做完整，但先不執行。** 等補齊交易所日曆後，下一輪再跑。

- catalog 的 `historical` 設為 `executable`，Fetcher 的 historical runner、回補 CLI 與「依 feed 取時區」都在本次完成並測試。
- 本次上線只啟動每日更新，不送出回補請求。
- 「必須有已發布日曆」同樣適用於回補：production 的日曆目前只有美股 2025–2028、港股 2026，更早的日期要先匯入日曆。

#### 每日抓取方式

- **逐檔呼叫 `historical-price-eod/full`**（`from` 與 `to` 都是目標日），每組每天各一次、共 10 次。只接受日期等於
  目標日的列；沒有該列時視為尚未發布，在重試窗口內重試。
- 不用 `eod-bulk`：連續呼叫會 429，兩組各自呼叫等於同一份全市場資料抓兩次，而且回應是 CSV、需要新的 raw 格式。
- 歷史回補同樣逐檔呼叫 `historical-price-eod/full`，`^GSPC` 從 1927 年起。
- 個股與 ETF 之後若改由 bulk 取得，目標日之後有分割生效的標的要改以單檔 `non-split-adjusted` 補抓。

#### 與現行排程設計的相容性

2026-11-20 核對程式碼：排程本來就以 feed 為單位。每條 feed 依自己的時區與時間排入當天工作
（`fetcher/src/findb_fetcher/full_market_runtime.py` 的 `_queued_cycle`），Backend 依 dataset 的市場建立工作、
凍結日曆並計算期限（`backend/app/services/feed_schedule.py`、`full_market.py`）。`scheduler_control` 的
`scheduled_local_time`、`timezone` 與 `slot_id` 只用於顯示與篩選，不決定執行時間。同一個 provider 的工作依序執行，
港股工作等待發布時不會擋住美股工作。

一個 provider 底下有不同市場與時區的 feed，需要調整：

- ~~歷史回補的「今天」以 provider 為單位~~：PR 1 已改為依 feed 的 calendar market 計算
  （`historical_backfill.py` 的 `FEED_TIMEZONES`）。`test_catalog_slot_migration.py` 仍要求一個 control 只有一個
  時段，隨啟用 FMP 的 PR 一起調整。
- ~~seed 以字串比大小挑「最早的 feed」~~：PR 2 改為把各 feed 的排程換算成同一天的 UTC 時刻比較
  （`backend/scripts/seed_data.py` 的 `earliest_feed`），FMP 的 control 會顯示台北 08:30。
- ~~排程時區必須等於交易所時區~~：PR 2 核對 Backend 與 Fetcher 的排程計算（`data_ready_at`、`latest_due_date`、
  期限、`next_scheduled_at`、Fetcher 規劃日期與發布窗口）都依 feed 自己的時區與 lag，移除這條測試限制，並以
  台北 08:30、lag 1 的指數 feed 補測試（`backend/tests/test_cross_timezone_schedule.py`）。
- ~~控制列的時段~~（PR 5）：Admin 的 `slot_id` 篩選改依各 feed 的 slot，FMP 在兩個 slot 都會出現；
  market-freshness 新增 `markets`。控制台每條 feed 顯示市場、時間、時區、抓當日或前幾日與下次執行時間，
  多市場的 control 只顯示一張卡（`HK / US`）；slot 依原決定不顯示。
  - 「未部署的 provider 不可 start」不做：每個 catalog provider 都有 consumers；Backend 先於 Fetcher 部署時
    若先 start，Fetcher 部署前檢查要求 controls 停止，不會在沒有 consumer 時抓取。
- **啟動資格以 provider 為單位**：任何一條 feed 沒就緒，整個 provider 都不能抓
  （`backend/app/services/full_market_admission.py`）。指數 feed 少，第一版沿用；之後 feed 變多再評估改成依 feed。
- `not_before` 依 dataset 的市場收盤時間計算（`backend/app/services/provider_work.py` 的 `daily_readiness`），
  FMP 指數直接適用。
- 之後若要加每日抓取窗口，必須以 feed 為單位，不能沿用 FactSet 那種 provider 層級的寫死窗口。

#### 必須有交易所日曆

**機制已完成**（PR 1）：catalog 的 feed 欄位 `calendar_required`，行為見
[Full market 維運](../operations/full_market.md)。指數 feed 在啟用 FMP 的 PR 設為 `true`。

- 建立 plan 時檢查（`create_plan`），在凍結決策之前；以 409 `CALENDAR_UNPUBLISHED` 回絕，Fetcher 收到後
  改為等待重試（`DAILY_CALENDAR_UNPUBLISHED`），不會變成永久的 `DAILY_SOURCE_REJECTED`。
- 同一天其他 feed 照常規劃：Fetcher 規劃同日 sibling 時跳過等待日曆的那條，港股日曆缺少不會卡住美股。
- 不做成啟動資格的阻擋條件：那是以 provider 為單位，一條 feed 缺日曆會讓整個 provider 都不能抓。
  缺日曆改由工作的 failure code 與缺漏告警的 `CALENDAR_UNPUBLISHED` 細節顯示。
- 歷史回補同樣檢查，範圍內有未發布的日期就整個拒絕。
- production 目前的日曆：美股 2025–2028、港股只有 2026（`backend/scripts/seed_production_calendars.py`），2027 年起
  港股指數要等日曆發布才會執行。

#### PR 切分

比照 HKEX：先做不登記 provider 的準備，最後一個 PR 才把 `fmp` 加進 catalog。catalog 一加入 provider，
Fetcher descriptors、infra inventory、部署與 host 檢查都會要求它存在，所以 catalog、migration 與 infra 必須同時上。

| PR | 內容 | 狀態 |
| --- | --- | --- |
| 1 | `calendar_required`、缺日曆時等待重試、歷史回補時區改依 feed | 已合併（#646） |
| 2 | Backend 準備：`asset_class` 加 `index`、名單成員可用 `^` 開頭的指數代號、`FMP_SKIP_NON_TRADING_DAYS`、seed 依 UTC 時刻挑最早的 feed、跨時區與 lag 1 的排程測試、`backfill_world_names.py` 改用 FMP 代號 | 已合併（#648） |
| 3 | Fetcher：FMP client 與 adapter、名單刷新、歷史回補經通用的 member runner（尚不登記到 `ADAPTERS`） | 已合併（#649） |
| 4 | 啟用：catalog 的 `fmp` 與兩條 feed、migration `e5a7c9b1d3f2`（dataset、stopped control、凍結 schedule inspection）、Serve 的指數 scope、Fetcher 登記、infra（bundle v7、secrets、部署腳本）、端到端驗收測試 | 已合併（#650），staging 已部署 |
| 5 | Dashboard 與 Admin：逐 feed 顯示市場、時間、時區與抓取日期，多市場 control 一張卡；slot 篩選依 feed | 本 PR |
| 6 | staging 驗收：只啟動 `fmp`，首日先設成單日 | 未開始 |

#### 實作要點

- **Backend**
  - `backend/configs/provider_catalog.v1.json` 新增 provider `fmp` 與各組指數 feeds；擴充 `FeedDefinition` 的
    `asset_class`（`index`，PR 2 已加）與「需要已發布日曆」設定，以 `backend/scripts/export_provider_catalog.py`
    重產生 artifacts。
  - Instrument 模型：`asset_class` 為 `index`、代號沿用 FMP，見「指數的資料模型」。名單成員只有 `index` 可以用 `^`
    開頭（PR 2）；market_eod contract 與 instruments 對代號沒有格式限制，normalizer 會轉成大寫。
  - ~~`backend/app/config.py` 新增 `FMP_SKIP_NON_TRADING_DAYS`~~（PR 2 已加，runtime configuration 中可省略）。
  - `backend/scripts/seed_data.py` 的 `SUPPORTED_DATASETS` 與 dataset 定義；registry、scheduler dataset、control
    `full_market_fmp_v1` 與 Source credential scope。
  - 完成期限固定為排程加 12 小時（`backend/app/provider_catalog.py` 的
    `COMPLETION_DEADLINE_HOURS_AFTER_SCHEDULE`）：港股組隔天 08:00 HKT，美股組台北時間 20:30。
  - ~~美股組是第一條 `target_date_lag_days` 為 1 的 feed，`data_ready_at`、`latest_due_date` 與期限計算要補測試~~
    （PR 2）。
  - 新增 control 與 feed 時，要一併提供新的凍結 schedule inspection（目前是 `b3d5f7a9c1e2`，由
    `scripts/preflight_catalog_schedule.py` 在每次部署前執行），否則會判為 `unknown_difference`、中止部署。
- **Fetcher**（PR 3 已完成以下兩項，登記到 `ADAPTERS` 留到啟用的 PR）
  - `providers/fmp.py`：client、`from_env`、contract mapping；key 只放在 `apikey` header，錯誤訊息遮蔽。
    - 每檔每天一次 `historical-price-eod/full`（`from` = `to` = 目標日），journal 只在回應已有目標日那一列時才重播，
      所以「尚未發布」的空回應不會被重用。
    - 開高低收四個值都必須有、為正且有限；高低順序交給 Backend DQ 判斷。
    - **成交量照 FMP 原值送出，0 就存 0**（Owner 於 2026-11-23 決定：沒有其他來源可以取代）。指數本身沒有
      成交量，FMP 的值也不是一致的數列，使用時要注意（2021-01 至 2026-11 比對）：
      - `^RUT`、`^NYA` 約九成交易日與 `^GSPC` 的值完全相同，yfinance 也一樣；
      - `^SOX`、`^VIX` 一律為 0；
      - `^NDX` 約是 yfinance 的 1/6；
      - `^HSCE` 依數量級是成交股數，不是成交金額。2024-10-08 起，FMP 沒有標示就把單位從「股」改成「千股」：
        2024-10-07 是 4,488,874,200，10-08 是 10,624,600（同日 `^HSI` 為 11,257,900,000）。
        換單位之後和 yfinance 相同，之前是 yfinance 的 1000 倍。每日抓取與回補都存原值，不換算。
  - `provider_adapters/fmp.py`：`ProviderAdapter` 子類；每檔一次 JSON 請求、一個 raw 物件，沿用 json raw 格式。
    - 兩組的指數清單寫在 adapter（`INDEX_UNIVERSES`）。名單刷新讀 FMP 的 `/stable/index-list`（帶金鑰，經
      `UniverseContext.http` 走 runtime 的 governed client），每個指數都要在清單上、幣別相符，否則中止刷新、
      保留上一份名單；這份清單就是名單的 evidence。名單生效日為排程時區當天減 `target_date_lag_days`，
      美股組因此是前一個紐約日。
    - 沒有目標日的列：每日工作視為尚未發布（`wait=publication`，在發布重試窗口內重試）；歷史回補記為
      `missing`／`no_source_row`，並引用 FMP 回的空回應。
    - 方案不涵蓋的指數（HTTP 402／403）只影響該檔；429 進入帳號冷卻；其他錯誤結束本輪、稍後重試。
    - 歷史回補沿用通用的 `MemberHistoricalRunner`，以「建立時的名單」（`current_release`）服務整個請求的
      每一天；runner 類別與 CLI 登記留到啟用的 PR。
    - raw 物件的 `source-symbol` metadata 接受開頭的 `^`（物件鍵只含代號的雜湊）；journal 的回應與 Raw R2
      相同，加入 `_R2_BACKED_RESPONSES` 隨 closed work 清理。
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

- 商品與跨資產的 instrument 模型：`asset_class`、市場代碼、交易日曆、幣別與代號格式。指數已定案，見「指數的資料模型」。
- 指數歷史回補的起始年份，以及要先匯入哪些年份的交易所日曆。
- 美股 515 檔的名單來源，以及每個 dataset 的 bounded universe。
- 階段 5 的批次順序。
- 授權條款是否允許內部再提供給下游專案。
