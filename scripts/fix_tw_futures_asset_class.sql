-- ===========================================================================
-- Prod cleanup: convert 75 TW futures instruments asset_class equity -> index
-- ===========================================================================
-- Background:
--   bulk_ingest_twstock.py 把 history/index/*.csv (75 個 TAIFEX 期貨合約)
--   透過 /api/v1/source/ingest/twstock/direct 匯入。
--   TWStockMultichartsNormalizer 會把所有 row 標 asset_class='equity',
--   所以 75 個期貨合約被誤分類為 equity。本 SQL 把它們改回 'index'。
--
-- Safety:
--   1. 用 BEGIN/COMMIT；先看 SELECT 筆數再 commit。
--   2. uniq constraint (asset_class, market, symbol) 不會撞，因為改前是
--      ('equity','TW',sym)、改後是 ('index','TW',sym)，前者唯一、後者預期空。
--      若有前次測試殘留，UPDATE 會失敗於 unique violation -> 整筆 ROLLBACK。
--   3. market_data_eod 透過 instrument_id FK 連結，asset_class 改了不影響
--      既有行情列。
-- ===========================================================================

-- ---- 1) Dry-run：預期 75 列，狀態都是 ('equity','TW') ----
SELECT asset_class, market, COUNT(*) AS n
FROM instruments
WHERE market = 'TW'
  AND symbol IN (
    'CAF1','CDF1','CFF1','CQF1','CRF1','CSF1','DFF1','DGF1','DHF1','DYF1',
    'DZF1','EEF1','EGF1','EHF1','EKF1','EMF1','EPF1','EXF1','EXF2','EYF1',
    'EZF1','FXF1','FXF2','KSF1','KUF1','LWF1','MXF1','MXF2','MYF1','NEF1',
    'NLF1','NYF1','OAF1','OBF1','OFF1','OGF1','OJF1','OKF1','OMF1','OOF1',
    'ORF1','PFF1','QGF1','QKF1','QPF1','RXF1','RZF1','SHF1','TMF1','TXF1',
    'TXF1_AO','TXF1_AV','TXF2','TXF2_AO','TXF2_AV','TXFC7_AO','TXFC7_AV',
    'TXFF6','TXFF6_AO','TXFF6_AV','TXFI6','TXFI6_AO','TXFI6_AV','TXFL6_AO',
    'TXFL6_AV','XAF1','XBF1','XIF1','YHF1','ZEF1','ZEF2','ZFF1','ZFF2',
    'ZGF1','ZHF1'
  )
GROUP BY asset_class, market
ORDER BY asset_class;

-- ---- 2) 確認上面回傳 (asset_class='equity', market='TW', n=75) 才往下執行 ----
BEGIN;

UPDATE instruments
SET asset_class = 'index',
    updated_at = NOW()
WHERE market = 'TW'
  AND asset_class = 'equity'
  AND symbol IN (
    'CAF1','CDF1','CFF1','CQF1','CRF1','CSF1','DFF1','DGF1','DHF1','DYF1',
    'DZF1','EEF1','EGF1','EHF1','EKF1','EMF1','EPF1','EXF1','EXF2','EYF1',
    'EZF1','FXF1','FXF2','KSF1','KUF1','LWF1','MXF1','MXF2','MYF1','NEF1',
    'NLF1','NYF1','OAF1','OBF1','OFF1','OGF1','OJF1','OKF1','OMF1','OOF1',
    'ORF1','PFF1','QGF1','QKF1','QPF1','RXF1','RZF1','SHF1','TMF1','TXF1',
    'TXF1_AO','TXF1_AV','TXF2','TXF2_AO','TXF2_AV','TXFC7_AO','TXFC7_AV',
    'TXFF6','TXFF6_AO','TXFF6_AV','TXFI6','TXFI6_AO','TXFI6_AV','TXFL6_AO',
    'TXFL6_AV','XAF1','XBF1','XIF1','YHF1','ZEF1','ZEF2','ZFF1','ZFF2',
    'ZGF1','ZHF1'
  );
-- 預期 UPDATE 75；如果 < 75，代表部分 symbol 不在 prod。先 ROLLBACK 釐清。

-- 後驗：必須是 75 列、(index, TW)
SELECT asset_class, market, COUNT(*)
FROM instruments
WHERE market = 'TW'
  AND symbol IN (
    'CAF1','CDF1','CFF1','CQF1','CRF1','CSF1','DFF1','DGF1','DHF1','DYF1',
    'DZF1','EEF1','EGF1','EHF1','EKF1','EMF1','EPF1','EXF1','EXF2','EYF1',
    'EZF1','FXF1','FXF2','KSF1','KUF1','LWF1','MXF1','MXF2','MYF1','NEF1',
    'NLF1','NYF1','OAF1','OBF1','OFF1','OGF1','OJF1','OKF1','OMF1','OOF1',
    'ORF1','PFF1','QGF1','QKF1','QPF1','RXF1','RZF1','SHF1','TMF1','TXF1',
    'TXF1_AO','TXF1_AV','TXF2','TXF2_AO','TXF2_AV','TXFC7_AO','TXFC7_AV',
    'TXFF6','TXFF6_AO','TXFF6_AV','TXFI6','TXFI6_AO','TXFI6_AV','TXFL6_AO',
    'TXFL6_AV','XAF1','XBF1','XIF1','YHF1','ZEF1','ZEF2','ZFF1','ZFF2',
    'ZGF1','ZHF1'
  )
GROUP BY asset_class, market;

COMMIT;  -- 對勁就 COMMIT；不對勁改成 ROLLBACK;
