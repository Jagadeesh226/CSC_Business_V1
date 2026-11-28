import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { ApiError, api, setApiHandlers } from './api'
import { jsonResponse } from '../test/utils'

function lastCall(fn: ReturnType<typeof vi.fn>) {
  const [url, init] = fn.mock.calls[fn.mock.calls.length - 1] as [string, RequestInit]
  return { url, init, headers: init.headers as Record<string, string> }
}

describe('api client', () => {
  beforeEach(() => {
    document.cookie = 'csc_csrf=token-123'
    setApiHandlers({})
  })
  afterEach(() => {
    document.cookie = 'csc_csrf=; expires=Thu, 01 Jan 1970 00:00:00 GMT'
    vi.unstubAllGlobals()
  })

  it('sends credentials and no CSRF header on GET, with query params', async () => {
    const fetchMock = vi.fn().mockResolvedValue(jsonResponse(200, { items: [] }))
    vi.stubGlobal('fetch', fetchMock)
    await api.get('/api/v1/customers', { query: { search: 'ravi', page_size: 5 } })
    const { url, init, headers } = lastCall(fetchMock)
    expect(url).toBe('/api/v1/customers?search=ravi&page_size=5')
    expect(init.credentials).toBe('include')
    expect(headers['X-CSRF-Token']).toBeUndefined()
  })

  it('sends the CSRF header from the csc_csrf cookie on non-GET requests', async () => {
    const fetchMock = vi.fn().mockResolvedValue(jsonResponse(200, {}))
    vi.stubGlobal('fetch', fetchMock)
    await api.post('/api/v1/auth/logout')
    const { init, headers } = lastCall(fetchMock)
    expect(init.method).toBe('POST')
    expect(init.credentials).toBe('include')
    expect(headers['X-CSRF-Token']).toBe('token-123')
  })

  it('401 becomes a session-ended error and calls the handler', async () => {
    const onSessionEnded = vi.fn()
    setApiHandlers({ onSessionEnded })
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(jsonResponse(401, { detail: 'Not authenticated' })))
    const err = await api.get('/api/v1/dashboard').catch((e: unknown) => e)
    expect(err).toBeInstanceOf(ApiError)
    expect((err as ApiError).message).toBe('Your session has ended. Please sign in again.')
    expect(onSessionEnded).toHaveBeenCalledOnce()
  })

  it('a public 401 keeps the server message and does not end the session', async () => {
    const onSessionEnded = vi.fn()
    setApiHandlers({ onSessionEnded })
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(jsonResponse(401, { detail: 'Invalid email or password.' })))
    const err = await api
      .post('/api/v1/auth/login', { body: { email: 'a@example.com', password: 'x' }, public: true })
      .catch((e: unknown) => e)
    expect((err as ApiError).message).toBe('Invalid email or password.')
    expect(onSessionEnded).not.toHaveBeenCalled()
  })

  it('403 "change your password first" triggers the password handler', async () => {
    const onPasswordChangeRequired = vi.fn()
    setApiHandlers({ onPasswordChangeRequired })
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(jsonResponse(403, { detail: 'You must change your password first.' })))
    const err = (await api.get('/api/v1/dashboard').catch((e: unknown) => e)) as ApiError
    expect(err.kind).toBe('password_change_required')
    expect(onPasswordChangeRequired).toHaveBeenCalledOnce()
  })

  it('other 403 shows a plain message, not the server text', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(jsonResponse(403, { detail: 'Admin access required.' })))
    const err = (await api.get('/api/v1/users').catch((e: unknown) => e)) as ApiError
    expect(err.message).toBe("You don't have access to this.")
  })

  it.each([404, 409, 410])('%i uses the server detail text', async (status) => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(jsonResponse(status, { detail: 'Server says no.' })))
    const err = (await api.get('/api/v1/dashboard').catch((e: unknown) => e)) as ApiError
    expect(err.status).toBe(status)
    expect(err.message).toBe('Server says no.')
  })

  it('422 maps validation items to field messages', async () => {
    const detail = [
      { loc: ['body', 'new_password'], msg: 'String should have at least 10 characters', type: 'x' },
    ]
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(jsonResponse(422, { detail })))
    const err = (await api
      .post('/api/v1/auth/change-password', { body: { current_password: 'a', new_password: 'b' } })
      .catch((e: unknown) => e)) as ApiError
    expect(err.kind).toBe('validation')
    expect(err.fieldErrors).toEqual({ new_password: 'String should have at least 10 characters' })
    expect(err.message).toBe('String should have at least 10 characters')
  })

  it('422 with a string detail uses it as the message', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(jsonResponse(422, { detail: 'Bad input.' })))
    const err = (await api.get('/api/v1/dashboard').catch((e: unknown) => e)) as ApiError
    expect(err.message).toBe('Bad input.')
  })

  it('network failure becomes a friendly error', async () => {
    vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new TypeError('Failed to fetch')))
    const err = (await api.get('/api/v1/dashboard').catch((e: unknown) => e)) as ApiError
    expect(err.kind).toBe('network')
    expect(err.message).toBe("Can't reach the server. Check your connection.")
  })

  it('never shows an HTML error page', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response('<html>Boom</html>', { status: 500 })))
    const err = (await api.get('/api/v1/dashboard').catch((e: unknown) => e)) as ApiError
    expect(err.message).not.toContain('<')
  })
})
