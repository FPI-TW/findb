# Production native 資料與回復驗收待辦

Production foundation 與既有 releases 的歷史由 Git 追溯。
Staging／production 已採用 [統一部署契約](../operations/deployment.md)，
不再沿用 GitHub Environment gate、bounded topology 或 schema v2 accepted replay。
現行操作契約見 [部署文件](../operations/deployment.md)、[production 維運](../operations/production.md)
與 [Full market runbook](../operations/full_market.md)。

尚未完成：

- Owner 於台北時間 2026-10-16 將香港排除本次 native 驗收；本次 staging Pilot 目標為其餘六條，
  尚未全部通過，因此 production native 驗收保持未開始。七條 feed 架構與香港失敗紀錄仍保留。
  R2 診斷 prefix 的鎖定與相同 bytes 重試已驗證；不能據此宣稱整個 bucket 或七條 feeds 通過。
  Twelve Data 的美國 6,018 筆、香港 2,828 筆目錄已成功發布，413 問題已排除；
  香港凍結商品 `0001` 的受控單次診斷得到 HTTP／provider code 400、`no_data_response`，
  指定 `exchange=HKEX` 的追加診斷亦為相同結果；官方將 XHKG 列為需授權或未支援市場，
  香港另列未解項目，不計入本次分母，也不標示通過。既有兩項 retry exhausted 不重設。
  美股 2026-10-15 排程為台北 10 月 16 日 05:00，本輪不等待過夜，待 Owner 回覆後接續。
  FinLab 的兩條每日 feed 已取得完整真實交付證據，連同 Shioaji 在本次範圍共 4／6（原七條範圍仍為 4／7），見正式測試文件。
  FinLab FileStorage 與跨 feed 目錄缺欄隔離修復已部署至 `50cb51faadd9`，三輪目錄 refresh、
  記憶體與資料保留驗證見正式測試文件；仍需多輪日常抓取穩定性驗證。
  Pilot 全部停止，不將目錄診斷當作日常抓取驗收。
  最新股票 `8908` 與 ETF `00415A` 仍為 mapping gap，需核對 provider 資料可用性；
  不將已發布目錄等同所有成員有資料，也不修改既有凍結樣本。
  TAIFEX 2026-10-15 Pilot 計畫有兩筆 canonical 寫入，另兩項已 retry exhausted，整條 feed 未通過；
  未完成項目為前一日目錄的 MTX:202610W2 sessions，需核對新計畫的目錄日期與到期契約；
  保留凍結成員、首次日期與 terminal checkpoint，不以重設狀態或無證據 no_data 掩蓋失敗。
- 新統一版在 production 的 accepted-record rollback 演練與可核對證據。
- 既有 missing／DQ alerts 的人工調查與修復；不得由部署成功自動結案。

完成項目需整併至正式維運文件並自此移除；native 驗收與部署健康 gate 各自報告。
