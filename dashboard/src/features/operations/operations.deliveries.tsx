import { useState } from "react"
import { OperationsTabs } from "../../components/OperationsTabs"
import type { MissingDelivery } from "../../lib/admin-api"
import type { AdminRole } from "../../lib/admin-governance-api"
import { DeliveryPlansPanel } from "./DeliveryPlansPanel"
import { DeliveryAlertsPanel } from "./DeliveryAlertsPanel"
import { HistoricalBackfillsPanel } from "./HistoricalBackfillsPanel"
import { PageIntro } from "./operations.shared"
import {
  deliveriesSearchSchema,
  type DeliveriesPageSearch,
} from "./operations.search"

export type DeliveriesSearchUpdate =
  | DeliveriesPageSearch
  | ((previous: DeliveriesPageSearch) => DeliveriesPageSearch)
const tabs = [
  { value: "plans", label: "全市場計畫" },
  { value: "alerts", label: "缺漏告警" },
  { value: "backfills", label: "歷史回補" },
] as const

export function DeliveriesPage({
  search: providedSearch,
  updateSearch: providedUpdate,
  role = "viewer",
}: {
  search?: DeliveriesPageSearch
  updateSearch?: (next: DeliveriesSearchUpdate) => void
  role?: AdminRole
} = {}) {
  const [localSearch, setLocalSearch] = useState(() =>
    deliveriesSearchSchema.parse({})
  )
  const search = providedSearch ?? localSearch
  const updateSearch = providedUpdate ?? setLocalSearch
  const selected =
    role === "viewer" && search.tab === "backfills" ? "plans" : search.tab
  const [seed, setSeed] = useState<MissingDelivery>()
  return (
    <>
      <PageIntro
        eyebrow="Completeness"
        title="交付監控"
        description="檢查全市場交付完整性、追蹤缺漏告警與管理歷史回補。"
      />
      <OperationsTabs
        label="交付監控功能"
        tabs={
          role === "viewer"
            ? tabs.filter(tab => tab.value !== "backfills")
            : tabs
        }
        selected={selected}
        onChange={tab => updateSearch({ ...search, tab })}
      >
        {tab =>
          tab === "plans" ? (
            <DeliveryPlansPanel
              search={search}
              updateSearch={updateSearch}
              active={selected === tab}
            />
          ) : tab === "alerts" ? (
            <DeliveryAlertsPanel
              search={search}
              updateSearch={updateSearch}
              active={selected === tab}
              role={role}
              onBackfill={row => {
                setSeed({ ...row })
                updateSearch({ ...search, tab: "backfills" })
              }}
            />
          ) : (
            <HistoricalBackfillsPanel
              search={search}
              updateSearch={updateSearch}
              active={selected === tab}
              role={role}
              seed={seed}
            />
          )
        }
      </OperationsTabs>
    </>
  )
}
