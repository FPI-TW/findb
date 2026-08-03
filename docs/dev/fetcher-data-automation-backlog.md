# Fetcher 資料自動化 backlog

本文件追蹤四時段自動化尚未完成的 provider、universe、contract、canonical
model 與產品口徑。排程時區固定為 `Asia/Taipei`：

| Slot          | 時間  | 範圍                                                 |
| ------------- | ----- | ---------------------------------------------------- |
| `us_0600`     | 06:00 | 美國、歐洲、英國、印度、新加坡、商品、美國宏觀與債券 |
| `global_0815` | 08:15 | Crypto、外匯、DXY、UTC／紐約日界連續市場             |
| `tw_1430`     | 14:30 | 台股、ETF、指數、法人資料與 WTX 日盤                 |
| `asia_1630`   | 16:30 | 中國、香港、日本、韓國、澳洲等亞太市場               |

## 現行安全邊界

- Manifest v2 與 SQLite state v2 已有四 slot、provider/dataset/work-item/target
  date identity、v1 原地遷移與 delivery metadata；v1 pilot 保持相容。
- `us_0600` 引用受治理的 NYSE 2026–2028 calendar 檔；超出 calendar
  coverage 時排程會 fail closed，不能以 weekday 猜測交易日。Calendar 必須在
  啟用 2029 年度前完成 review 與展延。
- `scheduled_for` 表示 slot 實際觸發日，與可能因時區、週末或假日回推的
  `target_data_date` 分開保存；retryable miss 在 grace deadline 前不會耗盡。
- 目前可執行的reviewed pilots為：`us_0600` Twelve Data `AAPL`、`MSFT`、`NVDA`；
  `tw_1430` FinLab `2330`、`2317`日線；以及Shioaji `2330`、`0050`、`0056`、
  `006201`當日分鐘線。這不代表全市場universe已獲授權。
- Daily scheduler 不得隱式執行五年 backfill。Backfill 必須使用獨立命令、state
  與分塊策略，且每一筆 Source request 小於 1 MB。
- Universe 只能來自 reviewed、versioned manifest。不得由名稱、現有 cache、
  公開知識或「約 N 檔」描述推導完整成分股。
- 同一 canonical row 只能有一個權威來源；台股股票與 ETF OHLCV 以 FinLab
  為主，Twelve Data 只補明列標的。
- FinLab SDK 以 `finlab==1.5.7` optional dependency 管理；官方 changelog
  顯示 `1.5.8` 導入 Firebase／瀏覽器登入，因此固定在前一版，保留團隊帳號僅能使用
  `FINLAB_API_TOKEN` 的 headless 流程。不得未經驗證升級；通用 Fetcher image
  不會安裝，production只由獨立FinLab image、Source key、state與container啟用。

## 第一階段 coverage matrix

`待驗證` 代表尚未以 production 帳戶取得並保存可去識別 fixture；不能據此啟用。

| 市場／資料        | Provider 與 endpoint                 | 明確 universe                                                                                                                                                                  | Slot          | Canonical 支援                                                  | 驗證結果／阻礙                                                          | 啟用驗收                                                                        |
| ----------------- | ------------------------------------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ | ------------- | --------------------------------------------------------------- | ----------------------------------------------------------------------- | ------------------------------------------------------------------------------- |
| 美股日線 pilot    | Twelve Data `/time_series`           | `AAPL`, `MSFT`, `NVDA`                                                                                                                                                         | `us_0600`     | `market_eod.v1`、`us_equity_eod`                                | 已有 adapter 與受限 universe                                            | 保持現有 contract、raw-first、retry 與 checkpoint 測試通過                      |
| 美國主要指數      | Twelve Data；需逐一驗證 entitlement  | `SPX`, `NDX`, `INDU`, `SOX`, `RTY Index`                                                                                                                                       | `us_0600`     | OHLC 可用 `market_eod.v1`；identifier mapping 待定              | 待驗證 symbol、instrument type、歷史深度                                | 每個 symbol 有真實 fixture、canonical mapping 與五日 staging 證據               |
| 美國 sector 指數  | Twelve Data／待驗證                  | `S5INFT`, `S5COND`, `S5TELS`, `S5RLST`, `S5INDU`, `S5MATR`, `S5FINL`, `S5HLTH`, `S5ENRS`, `S5UTIL`, `S5CONS Index`                                                             | `us_0600`     | OHLC shape 可支援                                               | entitlement 與 symbol syntax 待驗證                                     | reviewed universe、fixture、quota 與 canonical identity 全數通過                |
| M7                | Twelve Data `/time_series`           | `AAPL`, `MSFT`, `NVDA`, `AMZN`, `GOOGL`, `META`, `TSLA`                                                                                                                        | `us_0600`     | `market_eod.v1`                                                 | pilot 以外需擴大 hard cap                                               | 逐 symbol fixture、credit budget、無重複 canonical row                          |
| 商品              | Twelve Data `/quote`, `/time_series` | `XBR/USD`, `WTI/USD`, `XAU/USD`, `XAG/USD`, `HG1`                                                                                                                              | `us_0600`     | OHLC shape 可支援；spot／future identity 未定                   | slash symbol、spot/future、roll 口徑未決                                | 產品核准 identity；adapter fixture、currency/type、歷史深度與 quota 通過        |
| 外匯與 DXY        | Twelve Data `/quote`, `/time_series` | `DXY`, `EUR/USD`, `USD/JPY`, `GBP/USD`, `USD/CAD`, `USD/CHF`, `USD/SEK`, `AUD/USD`, `USD/TWD`, `USD/CNH`, `USD/KRW`, `USD/SGD`, `AUD/JPY`, `BRL/JPY`                           | `global_0815` | 既有 FX direct canonical path；Fetcher contract/universe 未泛化 | 現有 universe 禁止 slash symbol 且只接受 US equity                      | 精確 14 項 fixture matrix、FX contract routing、credit hard cap、跨日測試       |
| Crypto OHLCV      | Twelve Data `/quote`, `/time_series` | `BTC/USD`, `ETH/USD`, `XRP/USD`, `SOL/USD`, `ADA/USD`                                                                                                                          | `global_0815` | 既有 crypto direct canonical path；TD acquisition 未接          | slash symbol、provider mapping 待完成                                   | 五項真實 fixture、24/7 target-date policy、UTC/NY 日界測試                      |
| 台股股票／ETF     | FinLab                               | Reviewed pilot為`2330`、`2317`；全市場股票與ETF仍須由FinLab governed dataset產生                                                                                               | `tw_1430`     | Pilot使用`market_eod.v1`、`tw_equity_eod`；`tw_etf_eod`待擴充   | Pilot已有durable scheduler、raw-first bundle、完整two-row Source/canonical gate與隔離deployment；全市場membership/completeness未完成 | 先觀察pilot五個有效交易日；全市場另需governed membership、sanitized fixture、quota與完整性政策 |
| 台股補充標的      | Twelve Data、TWSE／TPEx API          | `TWII`, `TWB23`, `TWB28`, `0050&exchange=TWSE`, `SOX Index`；不得推導全部 `TWBxx`                                                                                              | `tw_1430`     | 部分 OHLC 可支援；法人／估值等不可硬塞 EOD                      | entitlement、TWSE/TPEx adapter、新 contract/model 待辦                  | 每個 endpoint fixture；與 FinLab 權威範圍互斥；無雙源覆寫                       |
| WTX 日盤          | FinLab                               | `WTX`                                                                                                                                                                          | `tw_1430`     | `futures_continuous_eod.v1`、`wtx_eod`                          | Backend normalizer 已有；Fetcher adapter 未完成                         | 單一 FinLab feed、交易日與日盤完整性測試、無 legacy Bloomberg 雙源              |
| 港股指數          | Twelve Data／Bloomberg               | `HSI`; `HSTECH Index`, `HSCEI Index`, `HSMSI Index`, `VHSI Index`; `HSCIIT`, `HSCICD`, `HSCICS`, `HSCIH`, `HSCIPC`, `HSCIMT`, `HSCIIN`, `HSCIFN`, `HSCIUT`, `HSCIEN`, `HSCITC` | `asia_1630`   | HK direct path 可支援；Fetcher provider 缺少                    | TD/Bloomberg entitlement 待驗證                                         | 明確 universe、真實 fixture、source ownership 與五日 staging                    |
| 港股個股          | Twelve Data／Bloomberg               | `2559`, `981`, `1347`, `6869`, `151`, `1398`, `5`, `700`, `941`, `857`, `388`, `9988`, `1810`, `3690`, `300`, `9999`, `9618`, `9961`, `1024`                                   | `asia_1630`   | HK equity direct path可支援                                     | `02559` 去零與 coverage、`300` A/H mapping 未決；不得補足「約 40 檔」   | identifier 決策、逐 symbol fixture、去重與 entitlement 通過                     |
| 陸股指數          | Twelve Data／Bloomberg               | `SHCOMP`, `SZCOMP`, `000300`, `399001`, `399006`, `000016`; optional `SHSZ300 Index` cross-check                                                                               | `asia_1630`   | CN index direct path 可支援                                     | composite 與舊口徑尚未定案                                              | 核准 index set、exchange mapping、fixture 與 source ownership                   |
| 陸股 sector／個股 | Twelve Data／Bloomberg               | `SH000908`–`SH000917`; `600519&exchange=SSE`, `300750&exchange=SZSE`                                                                                                           | `asia_1630`   | CN direct path 可支援                                           | 「市值前 N」沒有 N 或 approved membership source                        | 僅啟用明列項目；fixture、market mapping、quota 與五日 staging                   |

## 新 provider 與 contract/model

| 項目                                         | 需要的能力                                                                                                                                                                                                                              | 阻礙                                                              | 驗收條件                                                                                     |
| -------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------- | -------------------------------------------------------------------------------------------- |
| FinLab production scheduler                  | Reviewed two-symbol pilot已有headless SDK gateway、deterministic raw bundle、`full_snapshot` mapper、persistent retry/lease/checkpoint與Source terminal count gate                                                                         | 公開SDK只回傳DataFrame，raw artifact不是exact HTTP bytes；全市場governed universe與完整性政策尚未完成                 | 以獨立Source key/R2/state/container連續觀察五個有效交易日；再另案擴大universe                         |
| TWSE／TPEx                                   | `MI_INDEX`, `T86`, `STOCK_DAY_ALL`, `BWIBBU_ALL` 與 OTC fallback adapters                                                                                                                                                               | 法人、估值、類股不是 market EOD                                   | 先新增專用 contract、canonical model、Alembic、DQ 與 Serve schema                            |
| Bloomberg                                    | HK/CN、macro、yield、credit series acquisition                                                                                                                                                                                          | Backend 有 direct normalizer，但 Fetcher 無 provider              | provider adapter、entitlement matrix、sanitized fixture、rate limit/retry                    |
| Macro／yield                                 | `USGG3M`, `USGG2YR`, `USGG5YR`, `USGG10YR`, `USGG30YR`, `SOFRRATE`, `USGGBE02`, `PCE CYOY`, `MOVE`, `USYC2Y10`, `USYC3M10`, `SPX Index`, `IBXXAX73`, `IBXXAJ03`, `IBXXAJ32`, `C0A1 Index`, `C0A4 Index`, `LG30YW Index`, `BEBGYW Index` | bond entity vs macro scalar、direct OAS vs calculated spread 未決 | 核准 canonical identity／calculation owner，然後 contract、fixture 與 Serve 驗收             |
| FRED／Finnhub／Marketaux／CME 或 Atlanta Fed | macro backup、economic calendar、news、FedWatch                                                                                                                                                                                         | 尚無 provider 與 event/news canonical model                       | provider-neutral event/news contracts、去重、時區、revision 與 attribution 測試              |
| CMC／alt.me／Binance／Bybit／Deribit         | global metrics、fear-and-greed、perpetual OI、DVOL                                                                                                                                                                                      | source/caption 衝突、altcoin endpoint 與 DVOL endpoint 未定       | 產品決策後新增 snapshot／derivatives contracts、fixture 與 provider fallback 規則            |
| Instrument reference data                    | profile、market cap、sector、index membership、fundamentals                                                                                                                                                                             | 現有 InstrumentStats 不承載這些欄位                               | 新 contract/model/migration；point-in-time 與 membership effective-date 測試                 |

## 上線順序與觀察

Shioaji Taiwan-minute 已有四檔reviewed production-pilot scheduler、獨立container/state、
raw-first Source delivery與canonical count gate；原staging coordinator仍保留為bounded
one-shot驗證工具。完整production universe、跨sequence publication、archive/backfill與
Serve/Export publication仍在backlog。

1. 在staging以production credentials完成三provider的live preflight，確認獨立
   Source/R2/state/container與dataset registry。
2. 觀察reviewed pilots的contract、target-date、DST／假日、grace、retry、late recovery
   與 1 MB request 上限。
3. 完成FinLab與Shioaji全市場governed universe、publication barrier與completeness policy。
4. 使用獨立backfill/archive工作流完成五年資料，核對重複、日期缺口與source ownership。
5. 依序擴充`tw_1430`、`asia_1630`、`us_0600`、`global_0815`；每個slot至少觀察兩個
   有效交易日，全數啟用後再觀察一個完整交易週。

Reviewed pilots以外的項目，只有在entitlement/fixture、canonical mapping、DQ、API
freshness、alert、rollback與observability同時通過後，才能把manifest `enabled`改為
`true`；既有pilots的實際Environment activation仍須完成上述live觀察。
