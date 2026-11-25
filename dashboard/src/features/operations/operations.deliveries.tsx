import { useEffect, useState } from "react"
import { OperationsTabs } from "../../components/OperationsTabs"
import type { AdminRole } from "../../lib/admin-governance-api"
import { DeliveryAlertsPanel } from "./DeliveryAlertsPanel"
import {
  HistoricalBackfillsPanel,
  type BackfillSeed,
} from "./HistoricalBackfillsPanel"
import { ProviderWorkPanel } from "./ProviderWorkPanel"
import { UniverseCandidatesPanel } from "./UniverseCandidatesPanel"
import { UniverseFeedsPanel } from "./UniverseFeedsPanel"
import { PageIntro } from "./operations.shared"
import {
  deliveriesSearchSchema,
  type DeliveriesPageSearch,
} from "./operations.search"

export type DeliveriesSearchUpdate =
  | DeliveriesPageSearch
  | ((previous: DeliveriesPageSearch) => DeliveriesPageSearch)
const tabs = [
  { value: "alerts", label: "缺漏告警" },
  { value: "candidates", label: "Universe 名單" },
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
    search.tab === "plans" || (role === "viewer" && search.tab === "backfills")
      ? "alerts"
      : search.tab
  const [seed, setSeed] = useState<BackfillSeed>()
  // A draft carried in the URL from the completeness matrix fills the form once.
  useEffect(() => {
    if (!search.seed) return
    const [source = "", dataset_key = "", expected_data_date = ""] =
      search.seed.split("|")
    setSeed({ source, dataset_key, expected_data_date })
    updateSearch({ ...search, tab: "backfills", seed: "" })
  }, [search, updateSearch])
  return (
    <>
      <PageIntro
        eyebrow="Completeness"
        title="交付監控"
        description="處理缺漏告警、Universe 名單與歷史回補；各資料集每日是否完整請看「資料完整性」。"
      />
      <ProviderWorkPanel />
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
          tab === "alerts" ? (
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
          ) : tab === "candidates" ? (
            <>
              <UniverseFeedsPanel role={role} active={selected === tab} />
              <UniverseCandidatesPanel
                search={search}
                updateSearch={updateSearch}
                active={selected === tab}
              />
            </>
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
