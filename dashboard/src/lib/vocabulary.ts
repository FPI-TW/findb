/**
 * Display names for the stable keys the API returns. Every page shows a key as
 * `中文名(key)` through these helpers, so a name is defined once; filters,
 * URLs, requests and exports keep the bare key.
 */

/** Canonical asset classes (`backend/app/vocabulary.py` KNOWN_ASSET_CLASSES). */
export const ASSET_CLASS_NAMES: Readonly<Record<string, string>> = {
  bond: "債券",
  crypto: "加密貨幣",
  equity: "股票",
  etf: "ETF",
  future: "期貨",
  fx: "外匯",
  index: "指數",
  macro: "總經",
  mixed: "綜合",
}

/**
 * Datasets: every feed in the generated provider catalog, plus retired
 * datasets whose history and backfill requests are still shown.
 */
export const DATASET_NAMES: Readonly<Record<string, string>> = {
  hk_equity_eod: "港股日K",
  hk_equity_official_eod: "港股交易所日行情",
  hk_index_eod: "港股指數日K",
  tw_equity_bar_1m: "台股 1 分 K",
  tw_equity_eod: "台股日K",
  tw_equity_minute: "台股分鐘 K（已退役）",
  tw_equity_tick: "台股逐筆成交",
  tw_etf_bar_1m: "台股 ETF 1 分 K",
  tw_etf_eod: "台股 ETF 日K",
  tw_etf_minute: "台股 ETF 分鐘 K（已退役）",
  tw_etf_tick: "台股 ETF 逐筆成交",
  tw_futures_eod: "台灣期貨日K",
  tw_tpex_index_eod: "櫃買指數日K",
  tw_twse_index_eod: "加權指數日K",
  us_dollar_index_eod: "美元指數日K",
  us_equity_eod: "美股日K",
  us_index_eod: "美股指數日K",
}

/** `中文名(key)`; a key without a name is shown as is. */
export function withName(
  names: Readonly<Record<string, string>>,
  key: string
): string {
  const name = Object.hasOwn(names, key) ? names[key] : undefined
  return name ? `${name}(${key})` : key
}

export const assetClassLabel = (key: string) => withName(ASSET_CLASS_NAMES, key)

export const datasetLabel = (key: string) => withName(DATASET_NAMES, key)
