import type {
  CorporateActionRow,
  InstrumentLookupResponse,
  LookupItem,
  LookupResponse,
  LookupSearch,
  MacroLookupResponse,
  MacroObservationRow,
  PriceRow,
} from "./types"

const LOOKUP_ENDPOINTS = {
  instruments: "/api/v1/serve/lookup/instruments",
  macro: "/api/v1/serve/lookup/macro-series",
} as const

async function fetchJson<T>(url: string, signal?: AbortSignal): Promise<T> {
  const response = await fetch(url, {
    headers: { Accept: "application/json" },
    ...(signal ? { signal } : {}),
  })
  if (!response.ok) throw new Error(`伺服器回傳 HTTP ${response.status}`)
  return (await response.json()) as T
}

function lookupParams(
  search: LookupSearch,
  page = search.p,
  pageSize = search.ps
) {
  const params = new URLSearchParams({
    sort_by: search.sb,
    sort_dir: search.sd,
    page: String(page),
    page_size: String(pageSize),
  })
  const query = search.q.normalize("NFKC").trim()
  if (query) params.set("q", query)
  if (search.m !== "ALL") params.set("market", search.m)
  if (search.ds === "macro") {
    if (search.fq !== "ALL") params.set("frequency", search.fq)
    if (search.src !== "ALL") params.set("source", search.src)
  } else {
    if (search.ac !== "ALL") params.set("asset_class", search.ac)
    if (search.st !== "ALL") params.set("status", search.st)
  }
  return params
}

function assertLookupResponse(payload: LookupResponse): LookupResponse {
  if (
    payload.success !== true ||
    !Array.isArray(payload.data) ||
    !payload.pagination ||
    !payload.facets
  ) {
    throw new Error("Lookup API 回應格式不正確")
  }
  return payload
}

export async function loadLookupPage(
  search: LookupSearch,
  signal?: AbortSignal
) {
  const url = `${LOOKUP_ENDPOINTS[search.ds]}?${lookupParams(search)}`
  const payload =
    search.ds === "macro"
      ? await fetchJson<MacroLookupResponse>(url, signal)
      : await fetchJson<InstrumentLookupResponse>(url, signal)
  return assertLookupResponse(payload)
}

export async function loadAllLookupItems(
  search: LookupSearch,
  signal?: AbortSignal
) {
  const items: LookupItem[] = []
  let page = 1
  let totalPages = 1
  do {
    const url = `${LOOKUP_ENDPOINTS[search.ds]}?${lookupParams(
      search,
      page,
      200
    )}`
    const payload = assertLookupResponse(
      search.ds === "macro"
        ? await fetchJson<MacroLookupResponse>(url, signal)
        : await fetchJson<InstrumentLookupResponse>(url, signal)
    )
    items.push(...payload.data)
    totalPages = Math.max(1, payload.pagination.total_pages)
    page += 1
  } while (page <= totalPages)
  return items
}

function readData<T>(payload: unknown): T[] {
  if (
    typeof payload !== "object" ||
    payload === null ||
    !("data" in payload) ||
    !Array.isArray(payload.data)
  ) {
    throw new Error("API 回應格式不正確")
  }
  return payload.data as T[]
}

export async function loadPrices(id: string, signal?: AbortSignal) {
  return readData<PriceRow>(
    await fetchJson(
      `/api/v1/serve/eod/${encodeURIComponent(id)}?page_size=10`,
      signal
    )
  )
}

export async function loadCorporateActions(id: string, signal?: AbortSignal) {
  return readData<CorporateActionRow>(
    await fetchJson(
      `/api/v1/serve/corporate-actions/${encodeURIComponent(id)}?page_size=5`,
      signal
    )
  )
}

export async function loadMacroObservations(id: string, signal?: AbortSignal) {
  return readData<MacroObservationRow>(
    await fetchJson(
      `/api/v1/serve/macro/observations/${encodeURIComponent(id)}?page_size=10`,
      signal
    )
  )
}
