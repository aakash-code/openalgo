import { apiClient } from './client'

/** One ranked-list entry. CPR/first-candle fields only ever arrive on
 * `intraday_boost` -- the server enriches that list alone before responding. */
export interface TfListItem {
  symbol: string
  ltp: number
  prev_close: number
  change_pct: number
  score: number
  cpr_width_pct?: number | null
  cpr_bias?: 'bullish' | 'bearish' | null
  first_candle_range_pct?: number | null
}

export interface MarketPulseData {
  intraday_boost: TfListItem[]
  breakout_beacon: TfListItem[]
  high_powered_stocks: TfListItem[]
}

export interface MarketPulseResponse {
  status: 'success' | 'error'
  data?: MarketPulseData
  message?: string
}

/** Raw TradeFinder field names, passed through unmapped except `symbol`/CPR
 * which the backend adds. `param_3` is rfactor everywhere it appears. */
export interface SectorIndexItem {
  Symbol: string
  param_3: number
}

export interface SectorStockItem {
  Symbol: string
  symbol: string
  param_0: number
  param_1: number
  param_2: number
  param_3: number
  cpr_width_pct?: number | null
  cpr_bias?: 'bullish' | 'bearish' | null
}

export interface SectorScopeData {
  index: SectorIndexItem[]
  /** Keyed by `"<name>_r_factor"` -- strip that suffix before displaying. */
  sectors: Record<string, Record<string, SectorStockItem>>
}

export interface SectorScopeResponse {
  status: 'success' | 'error'
  data?: SectorScopeData
  message?: string
}

export const tradefinderApi = {
  getMarketPulse: async (apiKey: string): Promise<MarketPulseResponse> => {
    const response = await apiClient.post<MarketPulseResponse>('/tfmarketpulse', {
      apikey: apiKey,
    })
    return response.data
  },

  getSectorScope: async (apiKey: string): Promise<SectorScopeResponse> => {
    const response = await apiClient.post<SectorScopeResponse>('/tfsectorscope', {
      apikey: apiKey,
    })
    return response.data
  },
}
