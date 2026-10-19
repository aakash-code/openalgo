import { webClient } from './client'

export interface WriterFlowSession {
  trade_date: string
  underlying: string
  expiry: string
  minutes: number
  last_sample: string
}

export interface WriterFlowSessionsResponse {
  status: 'success' | 'error'
  recorder_enabled: boolean
  sessions: WriterFlowSession[]
  message?: string
}

export type FlowClass =
  | 'writing'
  | 'covering'
  | 'buying'
  | 'unwinding'
  | 'adding'
  | 'reducing'
  | 'steady'

export interface WriterFlowSide {
  cls: FlowClass
  bias: -1 | 0 | 1
  streak_minutes: number
  streak_oi_change: number
  steady_minutes: number
  oi: number
  window_change: Record<string, number | null>
}

export interface WriterFlowRow {
  strike: number
  atm: boolean
  ce: WriterFlowSide | null
  pe: WriterFlowSide | null
}

export interface WriterFlowEvent {
  time: string
  strike: number
  side: 'CE' | 'PE'
  kind: FlowClass
  minutes: number
  oi_change: number
  bias: -1 | 0 | 1
}

export interface WriterFlowResponse {
  status: 'success' | 'empty' | 'error'
  message?: string
  underlying?: string
  expiry?: string
  date?: string
  recorder_enabled?: boolean
  step_seconds?: number
  /** How open interest is quoted: quantity (NSE, BSE) or lots (MCX). */
  oi_unit?: 'quantity' | 'lots'
  spot?: number | null
  times?: string[]
  strikes?: number[]
  heat?: { ce: (number | null)[][]; pe: (number | null)[][] }
  heat_cls?: { ce: string[][]; pe: string[][] }
  totals?: { time: string; ce_change: number; pe_change: number; spot: number | null }[]
  ladder?: WriterFlowRow[]
  events?: WriterFlowEvent[]
}

export interface WriterFlowQuery {
  underlying: string
  expiry?: string
  step?: 'auto' | '60' | '180' | '300'
}

export const writerFlowApi = {
  sessions: async (): Promise<WriterFlowSessionsResponse> =>
    (await webClient.get<WriterFlowSessionsResponse>('/writerflow/api/sessions')).data,

  flow: async (q: WriterFlowQuery): Promise<WriterFlowResponse> =>
    (
      await webClient.get<WriterFlowResponse>('/writerflow/api/flow', {
        params: { underlying: q.underlying, expiry: q.expiry || undefined, step: q.step || 'auto' },
      })
    ).data,
}
