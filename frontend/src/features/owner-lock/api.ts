// Owner passcode lock (fork feature). See utils/owner_lock.py.
import { webClient } from '@/api/client'

export interface OwnerLockStatus {
  enabled: boolean
  unlocked: boolean
  expires_in: number
  unlock_minutes: number
}

export const ownerLockApi = {
  status: async (): Promise<OwnerLockStatus> =>
    (await webClient.get<OwnerLockStatus>('/owner-lock/status')).data,
  unlock: async (passcode: string) =>
    (await webClient.post('/owner-lock/unlock', { passcode })).data,
  lock: async () => (await webClient.post('/owner-lock/lock')).data,
  setPasscode: async (body: {
    new_passcode?: string
    current_passcode?: string
    login_password?: string
    remove?: boolean
  }) => (await webClient.post('/owner-lock/passcode', body)).data,
}
