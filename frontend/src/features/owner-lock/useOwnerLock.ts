import { useQuery, useQueryClient } from '@tanstack/react-query'
import { ownerLockApi } from './api'

export const OWNER_LOCK_KEY = ['owner-lock-status']

export function useOwnerLock() {
  const qc = useQueryClient()
  const query = useQuery({
    queryKey: OWNER_LOCK_KEY,
    queryFn: ownerLockApi.status,
    // Re-check each minute so an unlock that ran out locks the page again.
    refetchInterval: 60_000,
  })
  return {
    ...query,
    locked: Boolean(query.data?.enabled && !query.data?.unlocked),
    refresh: () => qc.invalidateQueries({ queryKey: OWNER_LOCK_KEY }),
  }
}
