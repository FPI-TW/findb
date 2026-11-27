# TWSE／TPEx provider（#647）

台灣官方來源：加權指數、櫃買指數、乖離率、三大法人市場彙總（金額）與個股買賣超（股數）。
本文件記錄已定事項、實測結果與剩下的工作；功能完成時整併進 `docs/` 的現行文件後刪除。

**只供內部使用**：資料來自臺灣證券交易所與證券櫃檯買賣中心的公開網頁，不對外提供。

## 已定事項（Owner，2026-11-24）

1. **兩個 provider**：`twse` 與 `tpex`，各自的 Source credential、control、consumer 與速率。
2. **乖離率**：(收盤 − SMA_n) / SMA_n × 100。
   - Serve 即時計算，不另存。
   - 均線固定 5／10／20／60／120／240。
   - 用原始收盤價。
   - 範圍是 TW 股票、ETF 與指數，其他市場回 422。
3. **指數**：代號 `^TWII` 與 `^TWOII`，不收 `^TSE50`。
4. **個股表整張收，含權證**。
   - 以代號儲存，`instrument_id` 可為空，不替權證建 instrument。
   - 對得到既有 TW equity／ETF 的列才連上 instrument。
5. **#564 FinLab 第二波**：與本項重疊的三項改由官方來源取得，已從 FinLab 第二波移除。
6. **歷史回補**：
   - 三大法人補最近一年。
   - 兩檔指數補兩年，讓 240 日乖離率一上線就算得出來。catalog 的 `historical_lookback_days` 設為 731。
7. **錯誤處理**：三大法人表只要有一列自相矛盾，當天整張表擋下（DQ error），等重抓。
8. **使用範圍**：只供內部使用。

## 設計

- **指數：一個 provider 一個 dataset**。兩個 dataset 是 `tw_twse_index_eod`（`^TWII`）與 `tw_tpex_index_eod`（`^TWOII`）。
  - 一個 dataset 不能有兩個 provider，原因有三：
    - seed 規定每個 dataset 只有一條 Source feed；
    - 排程投影只取第一條 feed；
    - 首日以 dataset 為單位。
  - Serve 允許同一個 `(market_eod, TW, index)` 範圍有多個 dataset，各 dataset 以自己寫入的資料計算涵蓋範圍（#715）。
  - 指數的月資料端點一次回整個月，Fetcher 在同一輪內重複使用，只在缺目標日時重抓。
- **三大法人：沿用 daily plan，每個（provider, 表）一個 dataset，每天一個固定成員**。
  - 四個 dataset：
    - `tw_twse_institutional_summary`（BFI82U）；
    - `tw_tpex_institutional_summary`；
    - `tw_twse_institutional_stock`（T86 `selectType=ALL`，含權證；`ALLBUT0999` 會排除權證）；
    - `tw_tpex_institutional_stock`（dailyTrade `sect=AL`）。
  - 成員是板別（`TWSE`／`TPEX`）。名單證據是不帶日期的請求，交易所會回最新一天的表。
  - 個股表轉成具名欄位後超過 Source 的 1 MB 上限，所以 rows JSON 存進 Raw R2，以 by-reference 交付，做法比照 `intraday_partition`。
- **官方「三大法人合計」不含外資自營商**。BFI82U 附註寫明「外資自營商買賣金額已計入自營商」。
- **完成判定**：兩個三大法人 contract 已加入 `eod_completion.MEMBER_EOD_SCHEMAS`；三大法人的 DQ error 會讓該成員判為 invalid。

## 實測（2026-11-23～27）

- **TLS**：httpx 預設設定與 Fetcher container（Python 3.13）連兩個主機都正常，不需要另外的 TLS 設定。
- **速率**：從 staging Fetcher 主機經 SSM 測試。
  - TWSE 擋的是密集請求：前 10 次間隔 5 秒、接著間隔 2 秒時，回 HTTP 200 加 HTML 封鎖頁，約 3.5 分鐘後解除。間隔 4、6、10 秒各 20 次都正常。catalog 設每分鐘 10 次。
  - TPEx 每秒 4 次也沒有限制。catalog 設每分鐘 30 次。
- **歷史範圍**：
  - TWSE 指數至少可查到 1999 年；TPEx 指數從 2000 年起。
  - T86 從 2012-05-02 起。欄位數：2012～14 年 12 欄、2016 年 16 欄、2018 年起 19 欄。
  - TPEx 三大法人端點約從 2018 年起才有資料。
- **近一年抽樣 52 天**：表頭完全一致，每個等式都成立。外資自營商每天都是 0。
- **指數 OHLC**：近兩年沒有開盤或收盤落在高低之外。
- **發布時間**：11-25～27 三個交易日，台北 13:30～19:30 每分鐘查詢一次。

  | 端點 | 第一次出現 | 最後一次變動 |
  | --- | --- | --- |
  | TWSE 加權指數 | 13:42／13:42／13:44 | 同左 |
  | TPEx 櫃買指數 | 13:51／13:48／13:49 | 同左 |
  | TWSE 三大法人彙總（BFI82U） | 15:06／15:12／15:03 | 同左 |
  | TWSE 三大法人個股（T86） | 16:20／16:18／16:21 | 之後內容不變，只有列的順序每次不同 |
  | TPEx 三大法人彙總、個股 | 約 15:03 | 每天 17:14～17:15 修正一次：外資買進、賣出金額調高，買賣超不變 |

  - 觀測時 T86 用的是 `ALLBUT0999`（約 1,356 列），正式抓取用 `ALL`。
  - 排程取最後一次變動時間加 15 分鐘，再進位到 15 分；期限一律是排程加 12 小時：

  | Feed | 排程（台北） | 期限 |
  | --- | --- | --- |
  | 兩檔指數 | 14:15 | 隔日 02:15 |
  | TWSE 彙總 | 15:30 | 隔日 03:30 |
  | TWSE 個股 | 16:45 | 隔日 04:45 |
  | TPEx 彙總、個股 | 17:30 | 隔日 05:30 |

## 進度

已合併：

| PR | 內容 |
| --- | --- |
| #702 | TW index Serve scope、`historical_lookback_days`、`TWSE_`／`TPEX_SKIP_NON_TRADING_DAYS` |
| #703 | `GET /serve/eod/bias-ratios` |
| #704 | Fetcher 指數 client 與 adapter（未登記） |
| #706 | 三大法人 contract、三張表（migration `37ed18886cab`）與 normalizer |
| #707 | Fetcher 三大法人彙總與個股表（未登記） |
| #709 | `GET /serve/institutional-flows` 與 `/institutional-flows/summary` |

**啟用一（本 PR）：provider 與兩條指數 feed**
- catalog 加入 `twse`／`tpex` 兩個 provider 與兩條指數 feed。
- migration `e28706e77614`：兩個 dataset 與兩個 stopped control。
- seed。
- Fetcher 登記 adapter 與 historical runner。
- Fetcher bundle v9：十七個 consumers、secrets、部署腳本。
- 部署順序見 [部署文件](../operations/deployment.md#twse-與-tpex-provider-加入)。
- 已合併為 #777，staging 已部署：
  - FinDB 與 Fetcher CD 成功；
  - 17 個 consumers 都在執行，重啟 0 次；
  - 告警已建立。

**啟用二（本 PR）：四張三大法人表**
- catalog 加入四條 feed，排程依上表。
- migration `54a916bdb18f`：
  - 把四個 dataset 接到既有 control；
  - 擴大 Source credential 範圍；
  - 建立新的凍結 inspection。
- seed、`_IMPLEMENTED_ADAPTERS`。
- 端到端管線測試：四個 dataset 都走完 Source → normalization → plan 完成 → Serve。
- Fetcher 的 `STOCK_COLUMNS` 與 contract 的 `x-findb-row-layout` 一致，已由 #707 的測試涵蓋。
- 部署見 [部署文件](../operations/deployment.md#twse-與-tpex-三大法人-feeds)。

## 剩下的工作

1. **staging 驗收**（兩個啟用都已部署，需 Owner 在 staging Dashboard 操作）：
   - 對 `twse`、`tpex` 各個 dataset 做 universe refresh，發布 baseline；
   - 建立 11-24～11-27 的短區間回補，確認 Source → raw → canonical → Serve 一致，並與官方網頁比對；
   - 之後只啟動 `twse`、確認下一個交易日的 daily 準時完成後停止，再對 `tpex` 做一次。
2. **production 上線**：
   - 順序是 FinDB、登記 credential、`tofu -target` 建 secrets、Fetcher；
   - Production 的 AWS 寫入由使用者執行腳本。
3. **回補**：指數兩年、三大法人一年。
4. **抽查與整併**：
   - 乖離率抽 2330 與 `^TWII` 數天，人工計算後比對；
   - 上線後抽查各值與官方網頁一致；
   - 文件整併進 `docs/architecture/provider_metadata.md`、`docs/operations/full_market.md`、`docs/api/api_usage_guide.md`，刪除本文件。
