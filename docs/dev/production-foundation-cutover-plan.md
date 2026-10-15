# Production native 資料與回復驗收待辦

Production foundation 與既有 releases 的歷史由 Git 追溯。
Staging／production 已採用 [統一部署契約](../operations/deployment.md)，
不再沿用 GitHub Environment gate、bounded topology 或 schema v2 accepted replay。
現行操作契約見 [部署文件](../operations/deployment.md)、[production 維運](../operations/production.md)
與 [Full market runbook](../operations/full_market.md)。

尚未完成：

- 七條 feeds 的 staging Pilot 真實交付驗收尚未全部通過，因此 production native 驗收保持未開始。
  R2 診斷 prefix 的鎖定與相同 bytes 重試已驗證；不能據此宣稱整個 bucket 或七條 feeds 通過。
  Twelve Data 分市場目錄抓取與 nginx 大小上限修正已部署，完整 universe 提交仍遭 Backend
  Source 大小檢查回傳 413；補齊 Backend 端點限制後仍須重驗。
  FinLab universe 仍有 mapping gaps，受控 SDK 診斷達到 120 秒截止，原因待查；不得直接發布 candidate。
  Shioaji 登入與目錄更新成功，minute 完整快照仍待排程後驗證。
  TAIFEX 2026-10-15 Pilot 計畫有兩筆 canonical 寫入，另兩項已 retry exhausted，整條 feed 未通過；
  保留凍結成員、首次日期與 terminal checkpoint，不以重設狀態掩蓋失敗。
- 新統一版在 production 的 accepted-record rollback 演練與可核對證據。
- 既有 missing／DQ alerts 的人工調查與修復；不得由部署成功自動結案。

完成項目需整併至正式維運文件並自此移除；native 驗收與部署健康 gate 各自報告。
