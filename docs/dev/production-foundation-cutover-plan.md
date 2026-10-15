# Production native 資料與回復驗收待辦

Production foundation 與既有 releases 的歷史由 Git 追溯。
Staging／production 已採用 [統一部署契約](../operations/deployment.md)，
不再沿用 GitHub Environment gate、bounded topology 或 schema v2 accepted replay。
現行操作契約見 [部署文件](../operations/deployment.md)、[production 維運](../operations/production.md)
與 [Full market runbook](../operations/full_market.md)。

尚未完成：

- 七條 feeds 的 staging Pilot 真實交付驗收尚未全部通過，因此 production native 驗收保持未開始。
  R2 診斷 prefix 的鎖定與相同 bytes 重試已驗證；不能據此宣稱整個 bucket 或七條 feeds 通過。
  Twelve Data 的美國 6,018 筆、香港 2,828 筆目錄已成功發布，413 問題已排除；
  香港日資料仍回報 source_error，美股當日排程尚未到，兩條原生交付仍待驗收。
  FinLab universe 仍有 mapping gaps，主機 kernel 已確認 runtime 遭 global OOM 終止；
  Pilot 已停止。大型 DataFrame 分批計量修復仍需部署並驗證實際記憶體、目標日期與商品可用性，
  不得把 OOM 當作 provider 無資料，也不得直接發布 candidate。
  TAIFEX 2026-10-15 Pilot 計畫有兩筆 canonical 寫入，另兩項已 retry exhausted，整條 feed 未通過；
  未完成項目為前一日目錄的 MTX:202610W2 sessions，需核對新計畫的目錄日期與到期契約；
  保留凍結成員、首次日期與 terminal checkpoint，不以重設狀態或無證據 no_data 掩蓋失敗。
- 新統一版在 production 的 accepted-record rollback 演練與可核對證據。
- 既有 missing／DQ alerts 的人工調查與修復；不得由部署成功自動結案。

完成項目需整併至正式維運文件並自此移除；native 驗收與部署健康 gate 各自報告。
