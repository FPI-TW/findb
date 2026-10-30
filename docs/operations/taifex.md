# TAIFEX 期貨日行情

`taifex / tw_futures_eod / futures_eod.v1` 抓取 TX、MTX、TMF、TE、TF 的實際月／週到期契約
EOD，一般（regular）與盤後（after_hours）分開保存；價差與 continuous 不在範圍內。
官方資料免帳號、免 provider secret，Fetcher 只持有 FinDB 簽發的 TAIFEX Source key。
啟用、治理與回補流程分別見 [Full market](full_market.md)、[資料維護](data_maintenance.md#taifex-futures-eod-回補)
與 [provider work](../architecture/provider_work.md)；本文件記錄官方來源行為、判定規則與排查。

## 官方來源行為

下列行為於台北 2026-10-30 以實際請求核對，變更時須更新本節與 parser 測試：

| 來源 | 行為 |
| --- | --- |
| [OpenAPI `DailyMarketReportFut`](https://openapi.taifex.com.tw/v1/DailyMarketReportFut) | 只提供最新一日 JSON，經 Cloudflare 快取（`cf-cache-status: HIT`）。10/29 報表的 `last-modified` 為台北 10/30 06:36，10/30 14:28 仍回傳 10/29。台北 17:00 的日常抓取因此通常是前一日資料，日期不符時改用歷史 CSV。 |
| [歷史 CSV `futDataDown`](https://www.taifex.com.tw/cht/3/futDataDown) | `POST down_type=1`、單一 `commodity_id` 與單日起訖。HTTP 200、`content-type: text/html;charset=MS950`、CP950 無 BOM；19 個中文欄位，資料列尾端多一個逗號，到期月份含尾端空白。 |
| 休市、未來日期、未知或尚未上市商品 | 回傳約 197 bytes、只有表頭的 CSV，HTTP 仍為 200。 |
| 分段發布 | 交易日當天 13:45 收盤後，該日 CSV 先只有盤後列（前一晚 15:00 至當日 05:00 已結束）。2026-10-30 每 10 分鐘輪詢，14:48 仍只有盤後列，14:58 已出現含結算價的一般時段列；17:00 排程有約兩小時餘裕，但發布延遲時仍可能遇到部分報表。 |

同一交易日的 latest JSON 與五份 CSV 解析後完全一致（2026-10-29：64 列、32 契約）。
盤後列一律沒有結算價與未平倉量；一般時段無成交但有結算價／未平倉量的列（例如 TX 202706）
仍是 data，不是 no_data。新上市契約可能首日只有一般時段，次日才出現盤後。

平台未公布數值配額，429 依 `Retry-After` 與帳號 cooldown 處理，不自行估計上限。

## 運作與判定

- 日常工作在 max(日曆收盤, 17:00 台北) 後才可領取。新計畫須使用同日期 published universe；
  Source 回傳 `taifex_dated_universe_required` 時，Fetcher 先讀 latest JSON，日期不符再下載五份
  分商品 CSV，raw-first 存 R2 後提交 universe。latest JSON 回應非 200（例如 403）時，該日常
  工作直接判定失敗，不改用 CSV；這是現行預期行為。
- 報表為空、格式不符、日期不符或**沒有任何一般時段列**，一律視為尚未發布：保存 date
  prerequisite，一小時後重試，不寫入 durable response、不提交 universe、不推論 no_data。
- 日期 D 的換倉規則只與 effective date 不晚於 D 的最新 published release 比較，避免 D 在
  D+1 已發布後重試時被判為倒退。依序處理時，2026-10-12 至 10-29 的實際週／月換倉（含 10/21
  月到期）皆符合自動發布規則。
- 計畫成員為報表中實際存在的「契約 × 時段」。成交量為 0 且 OHLC／結算價／未平倉量全空才是
  no_trade，並附報表 SHA-256 與原列摘錄；成員在報表中缺列時記為 blocked，不記 no_data。
- 交付時官方 408／429／5xx 或網路中斷屬於整份報表不可用：不扣成員失敗額度、不回報
  source_error，結束本輪並由一般 provider work retry 重排；403 等其他錯誤仍逐成員記 source_error，
  成員連續 8 次後轉 `retry_exhausted` 人工處理。
- Canonical 每商品一個 instrument、每實際契約一筆 `futures_contract`，行情以契約＋交易日＋
  時段為鍵；`expiry_date` 目前不填。
- 歷史回補每日期固定五份 CSV 加 whole-day manifest；任何商品報表為空或未發布完成，該 request
  fail closed 並取消後續日期，須修正日曆或縮小範圍後另建 request。

## 日曆維護

TAIFEX 使用獨立市場日曆，不沿用 TWSE settlement-only 分類（例如 2026-02-12／13 TAIFEX 休市）。
日曆以已部署服務的 published revision 為準，`backend/configs/calendars/` 只是初始 seed，
不隨後續 revision 更新，不得用來判斷環境現況。每年 1 月 1 日前須在各環境發布下一年度
revision；缺少日期時採週一至五開市預設，會把平日休市誤判為交易日：

- 日常：該日持續以 `DATED_UNIVERSE_PENDING` 每小時重試，截止後只出現一般 `*_INCOMPLETE`
  告警（`plan_id` 為空）。日曆修正後下一輪即依休市略過。
- 歷史：該日報表為空，整個 request 失敗並取消後續日期。

年度更新：TAIFEX 以 `https://www.taifex.com.tw/file/taifex/CHINESE/4/{year}Calendar.pdf`
公告。保存原檔 SHA-256，只取台灣指數期貨 regular 交易日期，經 Admin
`POST /admin/calendars/imports/json/preview` → apply → publish 建立完整年度 revision。

臨時停市（颱風等）：以官方 CSV 為準確認五商品皆為空表頭，並以 TWSE 同日資料交叉核對；
Operator 以 `PATCH /admin/calendars/TAIFEX/{trade_date}`（帶 `expected_revision` 與 reason）
建立 draft，再由 Owner 以 `POST /admin/calendars/TAIFEX/{year}/publish` 發布。
官方報表為空的已知平日休市：2024-07-24、07-25、10-02、10-03、10-31，2025-12-25，
2026-10-23；均未出現「只有盤後列」的最終形態。

## 排查

1. `findb-fetch-runtime` health 的 `date_prerequisites` 列出等待中的日期與原因；Dashboard
   「Universe 候選」與 `GET /admin/universes?status=candidate` 列出未自動發布的 candidate。
2. 以官方 CSV 判斷原因：空表頭代表休市或尚未上市；只有盤後列代表尚未發布完成，等待即可；
   完整報表但仍為 candidate 時，以 `GET /admin/universes/{release_id}/diff` 核對差異。
3. 舊 candidate 依 [Full market](full_market.md) 的 `reconcile_taifex_rollover.py` 先預覽再套用；
   不得以 staging 結果核准 production。

```bash
curl -sS -X POST https://www.taifex.com.tw/cht/3/futDataDown \
  --data 'down_type=1&commodity_id=TX&commodity_id2=&queryStartDate=2026/10/29&queryEndDate=2026/10/29' \
  | iconv -f cp950 -t utf-8 | awk -F, 'NR>1 && $3 !~ /\// {print $18}' | sort | uniq -c
```

尚未完成的上線項目見 [backlog](../dev/backlog.md#taifex-正式上線)。
