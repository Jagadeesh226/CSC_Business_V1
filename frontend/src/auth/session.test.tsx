import { afterEach, describe, expect, it, vi } from 'vitest'
import { screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { adminUser, emptyDashboard, emptyQueue, employeeUser, jsonResponse, renderApp, stubFetch } from '../test/utils'

afterEach(() => vi.unstubAllGlobals())

const dash = { 'GET /api/v1/dashboard': emptyDashboard, 'GET /api/v1/work-queue': emptyQueue }

async function signOutViaMenu() {
  await userEvent.click(await screen.findByRole('button', { name: /Ravi Employee|Asha Admin/ }))
  await userEvent.click(screen.getByRole('button', { name: 'Sign out' }))
}

function logoutCalls(fetchMock: ReturnType<typeof stubFetch>) {
  return fetchMock.mock.calls.filter(
    ([u, init]) => String(u).endsWith('/auth/logout') && (init as RequestInit).method === 'POST',
  )
}

describe('sign out', () => {
  it('calls the server, clears cached data and shows the login page', async () => {
    const fetchMock = stubFetch({
      'GET /api/v1/auth/me': employeeUser,
      'POST /api/v1/auth/logout': jsonResponse(200, {}),
      ...dash,
    })
    const { client } = renderApp('/')
    await screen.findByRole('heading', { name: 'Dashboard' })
    await waitFor(() => expect(client.getQueryData(['dashboard'])).toBeTruthy())
    await signOutViaMenu()
    expect(await screen.findByRole('heading', { name: 'Sign in' })).toBeTruthy()
    expect(logoutCalls(fetchMock)).toHaveLength(1)
    expect(client.getQueryData(['dashboard'])).toBeUndefined()
    expect(screen.queryByText(/did not reach the server/)).toBeNull()
  })

  it('treats a 401 from logout as already signed out', async () => {
    stubFetch({
      'GET /api/v1/auth/me': employeeUser,
      'POST /api/v1/auth/logout': jsonResponse(401, { detail: 'Not authenticated' }),
      ...dash,
    })
    renderApp('/')
    await screen.findByRole('heading', { name: 'Dashboard' })
    await signOutViaMenu()
    expect(await screen.findByRole('heading', { name: 'Sign in' })).toBeTruthy()
    expect(screen.queryByText(/did not reach the server/)).toBeNull()
    expect(screen.queryByText('Your session has ended. Please sign in again.')).toBeNull()
  })

  it.each([
    ['a network failure', () => { throw new TypeError('Failed to fetch') }],
    ['a server error', () => jsonResponse(500, { detail: 'Internal error' })],
    ['a CSRF rejection', () => jsonResponse(403, { detail: 'Missing or invalid CSRF token.' })],
  ])('warns and offers retry after %s, still clearing local data', async (_name, failure) => {
    let attempts = 0
    stubFetch({
      'GET /api/v1/auth/me': employeeUser,
      'POST /api/v1/auth/logout': () => {
        attempts += 1
        return attempts === 1 ? failure() : jsonResponse(200, {})
      },
      ...dash,
    })
    const { client } = renderApp('/')
    await screen.findByRole('heading', { name: 'Dashboard' })
    await signOutViaMenu()
    expect(await screen.findByText(/Sign-out did not reach the server/)).toBeTruthy()
    expect(client.getQueryData(['dashboard'])).toBeUndefined()
    await userEvent.click(screen.getByRole('button', { name: 'Try signing out again' }))
    await waitFor(() => expect(screen.queryByText(/Sign-out did not reach the server/)).toBeNull())
    expect(attempts).toBe(2)
  })

  it('keeps the warning when the retry also fails', async () => {
    stubFetch({
      'GET /api/v1/auth/me': employeeUser,
      'POST /api/v1/auth/logout': jsonResponse(500, { detail: 'Internal error' }),
      ...dash,
    })
    renderApp('/')
    await screen.findByRole('heading', { name: 'Dashboard' })
    await signOutViaMenu()
    await userEvent.click(await screen.findByRole('button', { name: 'Try signing out again' }))
    await waitFor(() => expect(screen.getByRole('button', { name: 'Try signing out again' })).toBeTruthy())
    expect(screen.getByText(/Sign-out did not reach the server/)).toBeTruthy()
  })
})

describe('sign in', () => {
  async function signIn(email: string) {
    await userEvent.type(await screen.findByLabelText('Email'), email)
    await userEvent.type(screen.getByLabelText('Password'), 'a-long-password')
    await userEvent.click(screen.getByRole('button', { name: 'Sign in' }))
  }

  it('returns to the page the visitor first asked for', async () => {
    stubFetch({
      'GET /api/v1/auth/me': jsonResponse(401, { detail: 'Not authenticated' }),
      'POST /api/v1/auth/login': jsonResponse(200, { user: employeeUser, csrf_token: 'x' }),
    })
    renderApp('/receipts')
    await signIn('ravi@example.com')
    expect(await screen.findByRole('heading', { name: 'Receipts' })).toBeTruthy()
  })

  it('clears cached data so a second user never sees the first user’s data', async () => {
    let dashboardCalls = 0
    stubFetch({
      'GET /api/v1/auth/me': jsonResponse(401, { detail: 'Not authenticated' }),
      'POST /api/v1/auth/login': jsonResponse(200, { user: adminUser, csrf_token: 'x' }),
      'GET /api/v1/dashboard': () => {
        dashboardCalls += 1
        return new Promise<Response>(() => {}) // never answers
      },
      'GET /api/v1/work-queue': emptyQueue,
    })
    const { client } = renderApp('/login')
    client.setQueryData(['dashboard'], emptyDashboard) // left behind by the previous user
    await signIn('asha@example.com')
    await screen.findByRole('heading', { name: 'Dashboard' })
    await waitFor(() => expect(dashboardCalls).toBe(1))
    expect(screen.getByText('Loading jobs...')).toBeTruthy()
    expect(screen.queryByText('Open jobs')).toBeNull()
  })
})

describe('session ended', () => {
  const ended = {
    'GET /api/v1/dashboard': jsonResponse(401, { detail: 'Not authenticated' }),
    'GET /api/v1/work-queue': jsonResponse(401, { detail: 'Not authenticated' }),
    'POST /api/v1/auth/login': jsonResponse(200, { user: employeeUser, csrf_token: 'x' }),
  }

  it('clears cached data when a request returns 401', async () => {
    stubFetch({ 'GET /api/v1/auth/me': employeeUser, ...ended })
    const { client } = renderApp('/')
    client.setQueryData(['palette', 'customers', 'ravi'], { items: [] })
    await screen.findByRole('heading', { name: 'Sign in' })
    expect(client.getQueryData(['palette', 'customers', 'ravi'])).toBeUndefined()
  })

  // Employee is on /receipts; a palette search gets a 401, so the session ends there.
  async function endSessionOnReceipts(login: object) {
    stubFetch({
      'GET /api/v1/auth/me': employeeUser,
      'GET /api/v1/customers': jsonResponse(401, { detail: 'Not authenticated' }),
      'GET /api/v1/jobs': jsonResponse(401, { detail: 'Not authenticated' }),
      'GET /api/v1/vehicles': jsonResponse(401, { detail: 'Not authenticated' }),
      'GET /api/v1/dashboard': emptyDashboard,
      'GET /api/v1/work-queue': emptyQueue,
      'POST /api/v1/auth/login': jsonResponse(200, { user: login, csrf_token: 'x' }),
    })
    renderApp('/receipts')
    await screen.findByRole('heading', { name: 'Receipts' })
    await userEvent.keyboard('{Control>}k{/Control}')
    await userEvent.type(await screen.findByRole('combobox'), 'ravi')
    await screen.findByRole('heading', { name: 'Sign in' })
    await userEvent.type(screen.getByLabelText('Email'), 'someone@example.com')
    await userEvent.type(screen.getByLabelText('Password'), 'a-long-password')
    await userEvent.click(screen.getByRole('button', { name: 'Sign in' }))
  }

  it('returns the same user to the page they were on', async () => {
    await endSessionOnReceipts(employeeUser)
    expect(await screen.findByRole('heading', { name: 'Receipts' })).toBeTruthy()
  })

  it('does not send a different user to the previous user\u2019s page', async () => {
    await endSessionOnReceipts(adminUser)
    expect(await screen.findByRole('heading', { name: 'Dashboard' })).toBeTruthy()
  })
})
