import { useQuery } from "@tanstack/react-query"
import { useServerFn } from "@tanstack/react-start"
import { useProtectedQueryScope } from "../../components/ProtectedQueryScope"
import {
  liveQueryOptions,
  useRegisterOperationsQuery,
} from "../../components/OperationsRefresh"
import { loadDeliveryResource } from "../../lib/admin.functions"
import type { DeliveryResourceRequest } from "../../lib/admin-api"

export function useDeliveryResource(
  request: DeliveryResourceRequest,
  enabled = true
) {
  const load = useServerFn(loadDeliveryResource)
  const scope = useProtectedQueryScope()
  const queryKey = ["operations", "delivery-resource", scope, request] as const
  useRegisterOperationsQuery(queryKey, enabled)
  return useQuery({
    ...liveQueryOptions,
    queryKey,
    enabled,
    queryFn: () => load({ data: request }),
    refetchInterval:
      request.resource === "scopes" ? false : liveQueryOptions.refetchInterval,
  })
}
