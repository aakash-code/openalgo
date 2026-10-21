import userEvent from '@testing-library/user-event'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type { MarketReadResponse } from '@/api/writer-flow'
import { marketReadAlerts, useMarketReadAlerts } from '@/hooks/useMarketReadAlerts'
import { cleanup, fireEvent, render, screen, waitFor } from '@/test/test-utils'
import { MarketReadPanel } from './MarketReadPanel'
import { RightRail } from './RightRail'

const api = vi.hoisted(() => ({
  read: vi.fn(),
  setReadSettings: vi.fn(),
}))
const sound = vi.hoisted(() => ({ readySound: vi.fn(), playAlertSound: vi.fn(() => true) }))
vi.mock('@/lib/trading/alertDelivery', () => sound)
vi.mock('@/api/writer-flow', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/api/writer-flow')>()),
  writerFlowApi: api,
}))

// 21-Oct-2026 at 10:23, as the server read it.
const READ: MarketReadResponse = {
  status: 'success',
  underlying: 'NIFTY',
  expiry: '27OCT26',
  date: '2026-10-21',
  minute: '10:23',
  headline:
    'Call writers lead NIFTY since 09:53; futures show shorts covering. 23,000 turned from support to resistance at 10:07.',
  state: {
    flow: {
      side: 'call',
      leaning: 'call',
      since: '09:53',
      net_lakh: -14.2,
      threshold_lakh: 11.2,
      ce_lakh: 9.1,
      pe_lakh: -5.1,
      delta_net_lakh: -8.3,
      tone: -1,
    },
    futures: {
      cls: 'short_covering',
      words: 'shorts covering',
      since: '10:16',
      price_change: 17,
      oi_change_lakh: -0.2,
      tone: 1,
    },
    levels: {
      support: { strike: 22900, oi_lakh: 63.4, peak_lakh: 63.4, from_peak_pct: 0 },
      resistance: { strike: 23000, oi_lakh: 137, peak_lakh: 140, from_peak_pct: -2.2 },
    },
    volatility: {
      vix: 15.13,
      vix_from_open: 0.36,
      vix_10m: 0.02,
      atm_iv: 14.04,
      atm_iv_10m: -0.08,
    },
    churn: { traded_lakh: 162.3, oi_change_lakh: 4.1, changing_hands: true },
    others: {
      BANKNIFTY: { side: 'call', since: '09:48', net: -0.9, threshold: 1 },
      SENSEX: { side: 'call', since: '09:52', net: -1.5, threshold: 2.9 },
    },
    price: { spot: 22945, open: 22993, prev_close: 22888.95, day_high: 23117.7, day_low: 22929.3 },
  },
  events: [
    {
      id: 'support_at:23,000:0918',
      kind: 'support_at',
      level: 'info',
      time: '09:18',
      epoch: 1,
      title: 'Support at 23,000',
      reason: '23,000 holds the most put open interest near the money.',
    },
    {
      id: 'flow_call::0955',
      kind: 'flow_call',
      level: 'major',
      time: '09:55',
      epoch: 2,
      title: 'Call writers took over NIFTY',
      reason: 'Since 09:53, near the money: put open interest -16.8 lakh, call +15.8 lakh.',
    },
  ],
  series: [
    { time: '09:16', epoch: 1, spot: 23048, net_lakh: 3, threshold_lakh: 9 },
    { time: '09:17', epoch: 2, spot: 23027, net_lakh: 12, threshold_lakh: 9 },
    { time: '09:18', epoch: 3, spot: 23059, net_lakh: -4, threshold_lakh: 9 },
  ],
  settings: { enabled: true, telegram: true, sound: true },
}

beforeEach(() => {
  api.read.mockResolvedValue(READ)
  api.setReadSettings.mockResolvedValue({
    status: 'success',
    settings: { enabled: true, telegram: true, sound: false },
  })
})
afterEach(() => {
  cleanup()
  vi.clearAllMocks()
})

describe('MarketReadPanel', () => {
  it('leads with the headline and says it is not a forecast', async () => {
    render(<MarketReadPanel />)
    expect(await screen.findByText(READ.headline as string)).toBeInTheDocument()
    expect(screen.getByText(/It is not a forecast/)).toBeInTheDocument()
    expect(screen.getByText('NIFTY at 10:23')).toBeInTheDocument()
  })

  it('shows each reading', async () => {
    render(<MarketReadPanel />)
    expect(await screen.findByText('Call writers lead')).toBeInTheDocument()
    expect(screen.getByText('shorts covering')).toBeInTheDocument()
    expect(screen.getByText('22,900')).toBeInTheDocument()
    expect(screen.getByText('India VIX 15.13')).toBeInTheDocument()
    expect(screen.getByText('Agree')).toBeInTheDocument()
    expect(screen.getByText(/positions are changing hands/)).toBeInTheDocument()
    expect(screen.getByRole('img', { name: /Price and net writer flow/ })).toBeInTheDocument()
  })

  it('says when the last minutes no longer agree with the lead', async () => {
    const state = READ.state as NonNullable<MarketReadResponse['state']>
    api.read.mockResolvedValue({
      ...READ,
      state: { ...state, flow: { ...state.flow, side: 'put', leaning: 'call', tone: 0 } },
    })
    render(<MarketReadPanel />)
    expect(await screen.findByText('Put writers lead, calls taking over')).toBeInTheDocument()
  })

  it('lists turns newest first and keeps level notes behind a toggle', async () => {
    render(<MarketReadPanel />)
    expect(await screen.findByText('Call writers took over NIFTY')).toBeInTheDocument()
    expect(screen.queryByText('Support at 23,000')).not.toBeInTheDocument()
    await userEvent.click(screen.getByRole('button', { name: 'Every level move' }))
    const items = screen.getAllByRole('listitem').map((li) => li.textContent)
    expect(items[0]).toContain('09:55')
    expect(items[1]).toContain('Support at 23,000')
  })

  it('switches sound off from its settings', async () => {
    render(<MarketReadPanel />)
    await screen.findByText(READ.headline as string)
    await userEvent.click(screen.getByRole('button', { name: 'Market read settings' }))
    await userEvent.click(await screen.findByRole('switch', { name: 'Sound and popup' }))
    await waitFor(() => expect(api.setReadSettings).toHaveBeenCalledWith({ sound: false }))
  })

  it('says when nothing is recorded yet', async () => {
    api.read.mockResolvedValue({
      status: 'empty',
      message: 'Nothing recorded for NIFTY on this day yet.',
    })
    render(<MarketReadPanel />)
    expect(
      await screen.findByText('Nothing recorded for NIFTY on this day yet.')
    ).toBeInTheDocument()
  })
})

describe('market read alerts', () => {
  const update = {
    underlying: 'NIFTY',
    minute: '09:55',
    headline: 'h',
    sound: true,
    events: READ.events ?? [],
  }

  it('sound only for major events', () => {
    expect(marketReadAlerts(update).map((a) => a.title)).toEqual([
      '09:55 Call writers took over NIFTY',
    ])
  })

  it('stay quiet when the trader turned sound off', () => {
    expect(marketReadAlerts({ ...update, sound: false })).toEqual([])
  })
})

describe('the rail', () => {
  it('opens the market read', async () => {
    const onSelect = vi.fn()
    render(<RightRail active={null} onSelect={onSelect} />)
    await userEvent.click(screen.getByRole('button', { name: 'Market read' }))
    expect(onSelect).toHaveBeenCalledWith('marketread')
  })
})

describe('the alert tone', () => {
  function Host() {
    useMarketReadAlerts(true)
    return null
  }

  it('is unlocked by the first click, so a turn that comes later is heard', () => {
    render(<Host />)
    expect(sound.readySound).not.toHaveBeenCalled()
    fireEvent.pointerDown(window)
    fireEvent.pointerDown(window)
    expect(sound.readySound).toHaveBeenCalledTimes(1)
  })
})
