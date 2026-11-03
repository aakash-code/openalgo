import { Lock } from 'lucide-react'
import { type FormEvent, type ReactNode, useState } from 'react'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { Input } from '@/components/ui/input'
import { ownerLockApi } from './api'
import { useOwnerLock } from './useOwnerLock'

function errorText(err: unknown): string {
  const data = (err as { response?: { status?: number; data?: { message?: string } } })?.response
  if (data?.status === 429) return 'Too many tries. Wait 15 minutes and try again.'
  return data?.data?.message || 'Could not check the passcode. Try again.'
}

/** The blue locked card with a passcode box. */
export function OwnerLockCard({
  title = 'Python strategies are locked',
  compact = false,
}: {
  title?: string
  compact?: boolean
}) {
  const { refresh } = useOwnerLock()
  const [code, setCode] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')

  const submit = async (e: FormEvent) => {
    e.preventDefault()
    setBusy(true)
    setError('')
    try {
      await ownerLockApi.unlock(code)
      setCode('')
      await refresh()
    } catch (err) {
      setError(errorText(err))
    } finally {
      setBusy(false)
    }
  }

  return (
    <Card
      className={`border-blue-500/40 bg-blue-500/10 ${compact ? '' : 'mx-auto mt-10 max-w-md'}`}
    >
      <CardHeader>
        <CardTitle className="flex items-center gap-2 text-blue-600 dark:text-blue-400">
          <Lock className="h-5 w-5" />
          {title}
        </CardTitle>
        <CardDescription>
          Enter the owner passcode to view or manage them. You can still watch running strategies
          and results.
        </CardDescription>
      </CardHeader>
      <CardContent>
        <form onSubmit={submit} className="flex gap-2">
          <Input
            type="password"
            autoComplete="off"
            placeholder="Owner passcode"
            aria-label="Owner passcode"
            value={code}
            onChange={(e) => setCode(e.target.value)}
          />
          <Button type="submit" disabled={busy || !code}>
            Unlock
          </Button>
        </form>
        {error && <p className="mt-2 text-sm text-destructive">{error}</p>}
      </CardContent>
    </Card>
  )
}

/** Hides children behind the lock card while the owner lock is on. */
export function OwnerLockGate({ children, title }: { children: ReactNode; title?: string }) {
  const { locked, isLoading } = useOwnerLock()
  if (isLoading) return null
  if (locked) return <OwnerLockCard title={title} />
  return <>{children}</>
}

/**
 * Shows the list read-only: the lock card on top and every control inside
 * the page disabled, since the server refuses them anyway.
 */
export function OwnerLockReadOnly({ children }: { children: ReactNode }) {
  const { locked } = useOwnerLock()
  if (!locked) return <>{children}</>
  return (
    <div>
      <div className="px-4 pt-4">
        <OwnerLockCard compact />
      </div>
      <div className="owner-locked-readonly [&_a]:pointer-events-none [&_button]:pointer-events-none [&_button]:opacity-40">
        {children}
      </div>
    </div>
  )
}
