import { afterEach, describe, expect, it, vi } from 'vitest'
import { onServerEvent, setServerEventSocket } from './serverEvents'

type Fn = (data: unknown) => void

function fakeSocket() {
  const on = new Map<string, Set<Fn>>()
  return {
    on: vi.fn((name: string, fn: Fn) => {
      if (!on.has(name)) on.set(name, new Set())
      on.get(name)?.add(fn)
    }),
    off: vi.fn((name: string, fn: Fn) => on.get(name)?.delete(fn)),
    emit(name: string, data: unknown) {
      for (const fn of [...(on.get(name) ?? [])]) fn(data)
    },
    listeners: (name: string) => on.get(name)?.size ?? 0,
  }
}

const cleanups: (() => void)[] = []
afterEach(() => {
  for (const stop of cleanups.splice(0)) stop()
  setServerEventSocket(null)
})

const listen = (name: string, fn: Fn) => {
  const stop = onServerEvent(name, fn)
  cleanups.push(stop)
  return stop
}

describe('server events for chart indicators', () => {
  it('delivers an event to a listener registered before the connection existed', () => {
    const heard = vi.fn()
    listen('writer_flow_update', heard)
    const socket = fakeSocket()
    setServerEventSocket(socket as never)
    socket.emit('writer_flow_update', { underlying: 'NIFTY' })
    expect(heard).toHaveBeenCalledWith({ underlying: 'NIFTY' })
  })

  it('carries listeners across a reconnect and leaves the old connection alone', () => {
    const heard = vi.fn()
    const first = fakeSocket()
    setServerEventSocket(first as never)
    listen('writer_flow_update', heard)
    const second = fakeSocket()
    setServerEventSocket(second as never)
    expect(first.listeners('writer_flow_update')).toBe(0)
    second.emit('writer_flow_update', { underlying: 'CRUDEOIL' })
    expect(heard).toHaveBeenCalledTimes(1)
  })

  it('stops delivering once the listener stops, and detaches when nobody listens', () => {
    const socket = fakeSocket()
    setServerEventSocket(socket as never)
    const a = vi.fn()
    const b = vi.fn()
    const stopA = listen('writer_flow_update', a)
    const stopB = listen('writer_flow_update', b)
    expect(socket.listeners('writer_flow_update')).toBe(1) // one relay, however many listen
    stopA()
    socket.emit('writer_flow_update', {})
    expect(a).not.toHaveBeenCalled()
    expect(b).toHaveBeenCalledTimes(1)
    stopB()
    expect(socket.listeners('writer_flow_update')).toBe(0)
  })

  it('keeps one throwing listener from silencing the rest', () => {
    const socket = fakeSocket()
    setServerEventSocket(socket as never)
    const after = vi.fn()
    listen('writer_flow_update', () => {
      throw new Error('broken indicator')
    })
    listen('writer_flow_update', after)
    socket.emit('writer_flow_update', {})
    expect(after).toHaveBeenCalledTimes(1)
  })

  it('refuses events that are not broadcasts about shared data', () => {
    expect(() => onServerEvent('force_logout', () => {})).toThrow(
      /not an event charts can listen to/
    )
    expect(() => onServerEvent('order_update', () => {})).toThrow()
  })
})
