import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'

const api = vi.hoisted(() => ({
  getBackgroundSetting: vi.fn(),
  setBackgroundSetting: vi.fn(),
}))
vi.mock('@/api/tradefinder', async (orig) => {
  const real = (await orig()) as Record<string, unknown>
  return {
    ...real,
    tradefinderApi: { ...(real.tradefinderApi as object), ...api },
  }
})
const toast = vi.hoisted(() => ({ success: vi.fn(), error: vi.fn() }))
vi.mock('@/utils/toast', () => ({ showToast: toast }))

import { BackgroundSwitch } from './TradeFinderPanel'

describe("TradeFinder's background updates switch", () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  it('shows what the server holds and switches it off', async () => {
    api.getBackgroundSetting.mockResolvedValue({ status: 'success', background_enabled: true })
    api.setBackgroundSetting.mockResolvedValue({ status: 'success', background_enabled: false })
    render(<BackgroundSwitch />)
    const sw = await screen.findByRole('switch', { name: 'TradeFinder background updates' })
    await waitFor(() => expect(sw).toHaveAttribute('aria-checked', 'true'))
    fireEvent.click(sw)
    await waitFor(() => expect(sw).toHaveAttribute('aria-checked', 'false'))
    expect(api.setBackgroundSetting).toHaveBeenCalledWith(false)
    expect(toast.success).toHaveBeenCalledWith('TradeFinder updates are off')
  })

  it('stays put and says so when the server cannot save', async () => {
    api.getBackgroundSetting.mockResolvedValue({ status: 'success', background_enabled: false })
    api.setBackgroundSetting.mockRejectedValue(new Error('down'))
    render(<BackgroundSwitch />)
    const sw = await screen.findByRole('switch', { name: 'TradeFinder background updates' })
    await waitFor(() => expect(sw).not.toBeDisabled())
    fireEvent.click(sw)
    await waitFor(() => expect(toast.error).toHaveBeenCalled())
    expect(sw).toHaveAttribute('aria-checked', 'false')
  })
})
