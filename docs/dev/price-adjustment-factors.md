# 還原價：調整因子表設計

> 狀態：設計上的決定已由 Owner 於 2026-11-19 確認（見「已定事項」），尚未實作。適用所有提供日線的 provider，
> 由 [FMP provider 導入規劃](fmp-provider.md) 提出；追蹤 issue：
> [#606](https://github.com/FPI-TW/findb/issues/606)。
> 完成後依 `docs/dev/` 的規則，把仍有效的內容整併到 `docs/` 的現行文件並刪除本檔。

## 已定事項

- 還原價以**調整因子**表示，不存 provider 的完整還原價歷史。
- `market_data_eod` 照舊只存原始成交價。
- 同一事件只存**一份**因子，依 `source_priority` 取勝出者。
- provider 修正因子時**只保留現值**，舊值留在 raw 層追溯。
- `total` 基準**接受各 provider 自己的涵蓋範圍**，不在欄位層硬性統一，差異寫在本檔。
- 台股第一版**只提供 `total` 基準**；`split` 基準等公司行動匯入後再補。

## 為什麼存因子就夠

2026-11-19 以三家 provider 的正式帳號實測：還原價都等於「原始價 × 每次事件的因子連乘」，兩次事件之間
「還原價 ÷ 原始價」固定不變。只存因子重建的誤差如下：

| Provider | 樣本 | 重建誤差 |
| --- | --- | --- |
| Twelve Data | AAPL、NVDA、O，2015–2026，約 9,000 日、195 次配息、3 次分割 | 最大 0.0000065，只有第 5 位小數的四捨五入 |
| FinLab | 全市場 2,800 檔、8,268,349 列 | 最大相對誤差 4.3×10⁻¹⁶；開高低收共用同一因子 |
| FMP 只調分割 | AAPL、NVDA、0005.HK、0131.HK、0637.HK，2015–2026 | 0 |
| FMP 分割＋股息 | 同上 | 收盤最大 0.0069、開高低最大 0.0113，在 FMP 自身四捨五入的 1–2 個單位內 |

## 各 provider 的調整價行為

| | Twelve Data | FMP | FinLab |
| --- | --- | --- | --- |
| 取得方式 | `time_series` 的 `adjust=none／splits／dividends／all` | `non-split-adjusted`（原始）、`full`（只調分割）、`dividend-adjusted`（分割＋股息） | `price:*`（原始）、`etl:adj_*`（合併調整） |
| 可提供的基準 | `split`、`total` | `split`、`total` | 只有 `total` |
| 調整方向 | 往回：最新一天等於原始價 | 往回 | **往前**：最新一天不等於原始價，歷史不改寫 |
| 精度 | 小數 5 位 | 調整價四捨五入，多為小數 2 位 | 完整浮點數 |
| `total` 涵蓋 | 分割、股息；分拆（spin-off）放在分割基準 | 分割、股息；分拆未確認 | 分割、除權息（含配股）、減資 |
| 因子能否由事件公式重現 | **不能**，所用價格不是除息前收盤 | 近期事件可以，長歷史不完全 | 事件表提供參考價，但以比值取得最精確 |
| 已知問題 | `/dividends` 一次最多回 100 筆 | 港股還原價的跳點常比 `dividends` 清單早一個交易日；開高低的誤差是收盤的兩倍 | 合併因子無法分出分割部分 |

因此因子一律**從 provider 的還原價分段比值取得**（`derivation = provider_ratio`），事件清單只用來找候選日期與
連結 `corporate_action`。

## 表：`price_adjustment_factor`

一列代表「一檔標的、一種基準、一個生效日」的一個因子。

| 欄位 | 型別 | 說明 |
| --- | --- | --- |
| `factor_id` | UUID，UUIDv7 | 主鍵 |
| `instrument_id` | UUID，FK `instruments` | |
| `basis` | String(10) | `split` 或 `total` |
| `effective_date` | Date | 新基準開始的第一個交易日；因子套用到這天**之前**的所有價格。取價格序列實際跳動的日子 |
| `price_factor` | NUMERIC(28, 18) | 這次事件的價格因子；一拆十為 0.1，配息約 0.9992，反分割大於 1 |
| `volume_factor` | NUMERIC(28, 18)，可為空 | 成交量因子；一拆十為 10，配息為 1，無法判定時為空 |
| `event_type` | String(30) | `split`、`reverse_split`、`cash_dividend`、`stock_dividend`、`capital_reduction`、`spin_off`、`rights_issue`、`mixed`、`unknown` |
| `corporate_action_id` | UUID，FK `corporate_action`，可為空 | 對應的事件；找不到時為空，由 DQ 標出 |
| `derivation` | String(30) | `provider_ratio`、`event_formula`、`provider_reported` |
| `evidence` | JSONB | 分段起訖日、天數、比值中位數與離散度、四捨五入上限、raw payload 參照 |
| `source` | String(50) | 例如 `twelve_data`、`fmp`、`finlab` |
| `source_priority` | Integer | 沿用 `corporate_action` 的語意與預設值 1000 |
| `source_fetched_at` | timestamptz | |
| `first_seen_at` | timestamptz | 第一次收到這個因子；修正時不變 |
| `asof_ts` | timestamptz | 最後一次確認或修正 |
| `run_id` | UUID，可為空 | |
| `created_at`、`updated_at` | timestamptz | |

約束：

- 唯一鍵 `(instrument_id, basis, effective_date)`。
- `price_factor > 0`；`volume_factor IS NULL OR volume_factor > 0`。
- `basis IN ('split', 'total')`。
- `basis = 'split'` 的列，其 `event_type` 只能是分割類：`split`、`reverse_split`、`spin_off`、`mixed`、`unknown`。
- 時間戳都是 UTC-aware，主鍵用 `app.utils.uuid7()`。

精度用 NUMERIC(28, 18)，不用行情表的 (20, 8)：配息因子如 0.99922810 只到小數 8 位，幾十次連乘後誤差會累積到
1e-6 等級，高於 Twelve Data 與 FinLab 的精度。

## 還原價的計算

```text
基準 B 在 t 日的還原價 = 原始價(t) × Π price_factor
  連乘只取 basis = B 且 effective_date > t 的列
基準 B 在 t 日的成交量 = 原始量(t) × Π volume_factor（同上條件）
```

- 最新一天的還原價等於原始價，三家都換算成這個方向。
- FinLab 往前調：比值 C(t) = 還原價 ÷ 原始價。事件因子 = C(生效日前一個交易日) ÷ C(生效日)。
- 每檔每年只有幾列，連乘在服務層用 `Decimal` 計算，不存累積因子，新事件只新增一列。
- 任一相關列的 `volume_factor` 為空時，成交量不調整，並在回應中標示。

Serve 的 `/eod` 新增參數 `adjust=none|split|total`，預設 `none`，回應標明所用基準。
沒有因子的標的（指數、外匯、商品）一律回原始值。

## 因子的取得與更新

- **初次建立**：每檔抓整段歷史的原始價與還原價，依比值分段取因子。
  - Twelve Data：每種基準每檔約 1 credit，`outputsize` 上限 5,000。
  - FMP：每檔一次呼叫。
  - FinLab：整表下載，`etl:adj_*` 每張計費約 33 MB。
- **每日更新**：
  - Twelve Data、FMP：先用事件端點找出新事件的候選標的，例如 FMP `dividends-calendar`、`splits-calendar`，或 Twelve Data `/dividends`、`/splits`。再對這些標的抓生效日前後的短窗口還原價，比值跳動即產生新因子。
  - FinLab：每日差分更新 `etl:adj_close` 後，全市場比對比值跳動。
- **定期複核**：定期對每檔重跑整段歷史的分段檢查，抓出 provider 回溯修正的因子並覆寫現值。

## DQ

| 規則 | 嚴重度 |
| --- | --- |
| `price_factor <= 0` 或基準與 `event_type` 不符 | error |
| 同一段內比值的離散度超過該 provider 的四捨五入上限（Twelve Data 1e-5、FinLab 1e-9、FMP 約 2 × 0.005 ÷ 價格），代表有未辨識的事件 | error |
| 因子沒有對應的 `corporate_action` | warning |
| `effective_date` 與對應事件的 `ex_date` 不同（例如 FMP 港股常差一天） | warning |
| 配息因子小於 0.5 或大於 1 | warning |

## 尚未決定

- 因子由 Fetcher 推導、以 contract 送到 Source，還是由 Backend 從送來的原始價與還原價序列推導。
  - 建議由 Fetcher 推導：三家的換算方式都是 provider 專屬知識。
  - Contract 帶因子與 `evidence`，原始價與還原價序列保存在 raw 層。
- 事件候選的每日來源，以及 Twelve Data `/splits` 每次 20 credits 的用量控制。
