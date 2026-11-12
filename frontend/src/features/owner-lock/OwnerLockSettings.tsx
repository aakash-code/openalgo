import { Lock, LockOpen } from 'lucide-react'
import { type FormEvent, useState } from 'react'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { showToast } from '@/utils/toast'
import { ownerLockApi } from './api'
import { useOwnerLock } from './useOwnerLock'

function errorText(err: unknown): string {
  const r = (err as { response?: { status?: number; data?: { message?: string } } })?.response
  if (r?.status === 429) return 'Too many tries. Wait 15 minutes and try again.'
  return r?.data?.message || 'Could not save. Try again.'
}

export default function OwnerLockSettings({ embedded = false }: { embedded?: boolean } = {}) {
  const { data, refresh } = useOwnerLock()
  const enabled = Boolean(data?.enabled)
  const [current, setCurrent] = useState('')
  const [next, setNext] = useState('')
  const [confirm, setConfirm] = useState('')
  const [busy, setBusy] = useState(false)

  const reset = () => {
    setCurrent('')
    setNext('')
    setConfirm('')
  }

  const save = async (e: FormEvent) => {
    e.preventDefault()
    if (next !== confirm) {
      showToast.error('The two new passcodes do not match.')
      return
    }
    setBusy(true)
    try {
      await ownerLockApi.setPasscode(
        enabled
          ? { current_passcode: current, new_passcode: next }
          : { login_password: current, new_passcode: next }
      )
      showToast.success(enabled ? 'Passcode changed.' : 'Owner passcode is on.')
      reset()
      await refresh()
    } catch (err) {
      showToast.error(errorText(err))
    } finally {
      setBusy(false)
    }
  }

  const remove = async () => {
    setBusy(true)
    try {
      await ownerLockApi.setPasscode({ current_passcode: current, remove: true })
      showToast.success('Owner passcode removed. Nothing is locked now.')
      reset()
      await refresh()
    } catch (err) {
      showToast.error(errorText(err))
    } finally {
      setBusy(false)
    }
  }

  const lockNow = async () => {
    await ownerLockApi.lock()
    await refresh()
    showToast.success('Locked.')
  }

  return (
    <div className={embedded ? 'space-y-6' : 'container mx-auto max-w-2xl space-y-6 py-6'}>
      <div>
        {/* Inside Profile the tab already names the page. */}
        {!embedded && <h1 className="text-2xl font-semibold">Owner Lock</h1>}
        <p className="text-sm text-muted-foreground">
          Lets you share this login for demos. While locked, nobody can open, copy, export, edit,
          start, stop, schedule or delete your Python strategies, see the API key, or change the
          login password. Running strategies, positions, orders and results stay visible.
        </p>
      </div>

      <Card>
        <CardHeader>
          <CardTitle className="flex items-center gap-2">
            {enabled ? <Lock className="h-5 w-5" /> : <LockOpen className="h-5 w-5" />}
            {enabled ? 'Owner passcode is on' : 'Owner passcode is off'}
          </CardTitle>
          <CardDescription>
            {enabled
              ? data?.unlocked
                ? `Unlocked in this browser. It locks again after ${data?.unlock_minutes ?? 30} minutes, or when you log out.`
                : 'Locked in this browser.'
              : 'Set a passcode to turn the lock on.'}
          </CardDescription>
        </CardHeader>
        <CardContent className="space-y-4">
          {enabled && data?.unlocked && (
            <Button variant="outline" onClick={lockNow}>
              Lock now
            </Button>
          )}
          <form onSubmit={save} className="space-y-3">
            <div className="space-y-1">
              <Label htmlFor="ol-current">
                {enabled ? 'Current passcode' : 'Your login password'}
              </Label>
              <Input
                id="ol-current"
                type="password"
                autoComplete="off"
                value={current}
                onChange={(e) => setCurrent(e.target.value)}
              />
            </div>
            <div className="space-y-1">
              <Label htmlFor="ol-new">
                New passcode (at least 6 characters, not your login password)
              </Label>
              <Input
                id="ol-new"
                type="password"
                autoComplete="new-password"
                value={next}
                onChange={(e) => setNext(e.target.value)}
              />
            </div>
            <div className="space-y-1">
              <Label htmlFor="ol-confirm">Type the new passcode again</Label>
              <Input
                id="ol-confirm"
                type="password"
                autoComplete="new-password"
                value={confirm}
                onChange={(e) => setConfirm(e.target.value)}
              />
            </div>
            <div className="flex gap-2">
              <Button type="submit" disabled={busy || !current || !next}>
                {enabled ? 'Change passcode' : 'Turn on'}
              </Button>
              {enabled && (
                <Button
                  type="button"
                  variant="destructive"
                  disabled={busy || !current}
                  onClick={remove}
                >
                  Remove passcode
                </Button>
              )}
            </div>
          </form>
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle className="text-base">What the lock does not cover</CardTitle>
        </CardHeader>
        <CardContent className="space-y-1 text-sm text-muted-foreground">
          <p>
            Anyone with this login can still place and close orders, and use Flow, the Strategy
            module and the chart terminal.
          </p>
          <p>
            The chart terminal needs your API key to stream prices, so a technical person could
            still read it from the browser. Give demo users a sandbox setup, or rotate the API key
            after a demo.
          </p>
          <p>Anyone with access to the server itself can read everything.</p>
        </CardContent>
      </Card>
    </div>
  )
}
