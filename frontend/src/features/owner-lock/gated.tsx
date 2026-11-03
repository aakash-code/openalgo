import type { ComponentType } from 'react'
import { OwnerLockGate, OwnerLockReadOnly } from './OwnerLockGate'

type Mod = { default: ComponentType<Record<string, unknown>> }

/**
 * Wraps a lazily imported page in the owner lock, so App.tsx changes only
 * its lazy() line and its <Route> lines stay as upstream wrote them.
 */
export function gated(mode: 'gate' | 'readonly', title?: string) {
  return (mod: Mod): Mod => {
    const Page = mod.default
    const Wrapped = (props: Record<string, unknown>) =>
      mode === 'readonly' ? (
        <OwnerLockReadOnly>
          <Page {...props} />
        </OwnerLockReadOnly>
      ) : (
        <OwnerLockGate title={title}>
          <Page {...props} />
        </OwnerLockGate>
      )
    return { default: Wrapped }
  }
}
