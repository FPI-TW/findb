import { resolvePublicApiUrl } from "./api-url"
import type {
  EodRow,
  Instrument,
  InstrumentLookupResponse,
  LookupSearch,
  MinuteRow,
} from "./types"

const INSTRUMENTS_ENDPOINT = "/api/v1/serve/instruments"

async function fetchJson<T>(url: string, signal?: AbortSignal): Promise<T> {
  const response = await fetch(resolvePublicApiUrl(url), {
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
  if (search.ac !== "ALL") params.set("asset_class", search.ac)
  if (search.st !== "ALL") params.set("status", search.st)
  return params
}

function assertLookupResponse(payload: InstrumentLookupResponse) {
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
  return assertLookupResponse(
    await fetchJson<InstrumentLookupResponse>(
      `${INSTRUMENTS_ENDPOINT}?${lookupParams(search)}`,
      signal
    )
  )
}

export async function loadAllLookupItems(
  search: LookupSearch,
  signal?: AbortSignal
) {
  const items: Instrument[] = []
  let page = 1
  let totalPages = 1
  do {
    const payload = assertLookupResponse(
      await fetchJson<InstrumentLookupResponse>(
        `${INSTRUMENTS_ENDPOINT}?${lookupParams(search, page, 100)}`,
        signal
      )
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

export async function loadEod(id: string, signal?: AbortSignal) {
  return readData<EodRow>(
    await fetchJson(
      `/api/v1/serve/eod?instrument_id=${encodeURIComponent(id)}&page_size=10`,
      signal
    )
  )
}

export async function loadMinute(id: string, signal?: AbortSignal) {
  return readData<MinuteRow>(
    await fetchJson(
      `/api/v1/serve/minute?instrument_id=${encodeURIComponent(id)}&page_size=10`,
      signal
    )
  )
}
