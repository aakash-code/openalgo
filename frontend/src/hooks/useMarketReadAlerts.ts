/**
 * A tone and a popup for the market read's major events, while /trading is open.
 *
 * Mounted by the page rather than the panel, so a turn is heard with the panel
 * closed: the point is to look up from the chart when something changes, not
 * to have to be watching the panel already. Only major events (the lead
 * changing hands, a level turning, India VIX jumping) sound; the rest wait in
 * the panel's timeline. The trader's sound switch arrives with each event.
 */

import { useEffect } from 'react'
import type { MarketReadUpdate } from '@/api/writer-flow'
import { onServerEvent } from '@/lib/serverEvents'
import { playAlertSound, readySound } from '@/lib/trading/alertDelivery'
import { showToast } from '@/utils/toast'

export function marketReadAlerts(update: MarketReadUpdate): { title: string; reason: string }[] {
  if (!update?.sound || !Array.isArray(update.events)) return []
  return update.events
    .filter((e) => e.level === 'major')
    .map((e) => ({ title: `${e.time} ${e.title}`, reason: e.reason }))
}

export function useMarketReadAlerts(enabled: boolean): void {
  useEffect(() => {
    if (!enabled) return
    // A browser keeps audio silent until the page has been clicked or typed in,
    // and a turn arrives with nobody touching the page. The first gesture
    // unlocks it, so the tone is heard when the turn comes.
    const unlock = () => readySound()
    window.addEventListener('pointerdown', unlock, { once: true })
    window.addEventListener('keydown', unlock, { once: true })
    const off = onServerEvent('market_read_update', (data) => {
      const alerts = marketReadAlerts(data as MarketReadUpdate)
      if (alerts.length === 0) return
      playAlertSound()
      for (const a of alerts)
        showToast.info(a.title, undefined, { description: a.reason, duration: 15_000 })
    })
    return () => {
      off()
      window.removeEventListener('pointerdown', unlock)
      window.removeEventListener('keydown', unlock)
    }
  }, [enabled])
}
