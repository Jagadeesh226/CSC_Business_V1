import { afterEach, describe, expect, it, vi } from 'vitest'
import { screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { adminUser, emptyDashboard, emptyQueue, employeeUser, jsonResponse, renderApp, stubFetch } from '../test/utils'

afterEach(() => vi.unstubAllGlobals())

describe('forced password change', () => {
  const forced = { ...employeeUser, must_change_password: true }

  it('keeps the user on the change-password page', async () => {
    stubFetch({ 'GET /api/v1/auth/me': forced })
    renderApp('/jobs')
    expect(await screen.findByRole('heading', { name: 'Change password' })).toBeTruthy()
    expect(screen.getByText(/must choose a new password/)).toBeTruthy()
    expect(screen.queryByRole('navigation', { name: 'Main' })).toBeNull()
  })

  it('validates and submits the new password, then opens the dashboard', async () => {
    let me: object = forced
    const fetchMock = stubFetch({
      'GET /api/v1/auth/me': () => jsonResponse(200, me),
      'POST /api/v1/auth/change-password': () => {
        me = employeeUser
        return jsonResponse(200, employeeUser)
      },
      'GET /api/v1/dashboard': emptyDashboard,
      'GET /api/v1/work-queue': emptyQueue,
    })
    renderApp('/change-password')
    await userEvent.type(await screen.findByLabelText('Current password'), 'temp-password-1')
    await userEvent.type(screen.getByLabelText('New password'), 'short')
    await userEvent.type(screen.getByLabelText('Type the new password again'), 'short')
    await userEvent.click(screen.getByRole('button', { name: 'Change password' }))
    expect(await screen.findByText('The new password must be at least 10 characters.')).toBeTruthy()

    await userEvent.clear(screen.getByLabelText('New password'))
    await userEvent.clear(screen.getByLabelText('Type the new password again'))
    await userEvent.type(screen.getByLabelText('New password'), 'a-much-longer-one')
    await userEvent.type(screen.getByLabelText('Type the new password again'), 'a-much-longer-one')
    await userEvent.click(screen.getByRole('button', { name: 'Change password' }))
    expect(await screen.findByRole('heading', { name: 'Dashboard' })).toBeTruthy()
    const post = fetchMock.mock.calls.find(([, init]) => (init as RequestInit)?.method === 'POST')
    expect(JSON.parse((post![1] as RequestInit).body as string)).toEqual({
      current_password: 'temp-password-1',
      new_password: 'a-much-longer-one',
    })
  })
})

describe('navigation by role', () => {
  const routes = { 'GET /api/v1/dashboard': emptyDashboard, 'GET /api/v1/work-queue': emptyQueue }

  it('hides admin items from employees', async () => {
    stubFetch({ 'GET /api/v1/auth/me': employeeUser, ...routes })
    renderApp('/')
    const nav = await screen.findByRole('navigation', { name: 'Main' })
    expect(within(nav).getByRole('link', { name: 'Customers' })).toBeTruthy()
    for (const name of ['Services', 'Users', 'Audit log', 'Background work']) {
      expect(within(nav).queryByRole('link', { name })).toBeNull()
    }
  })

  it('shows admin items to admins', async () => {
    stubFetch({ 'GET /api/v1/auth/me': adminUser, ...routes })
    renderApp('/')
    const nav = await screen.findByRole('navigation', { name: 'Main' })
    for (const name of ['Services', 'Users', 'Audit log', 'Background work']) {
      expect(within(nav).getByRole('link', { name })).toBeTruthy()
    }
  })

  it('shows an "Admins only" page to an employee who opens an admin URL', async () => {
    stubFetch({ 'GET /api/v1/auth/me': employeeUser })
    renderApp('/users')
    expect(await screen.findByRole('heading', { name: 'Admins only' })).toBeTruthy()
  })

  it('opens placeholder pages for other screens', async () => {
    stubFetch({ 'GET /api/v1/auth/me': employeeUser })
    renderApp('/receipts')
    expect(await screen.findByText('Coming in a later step.')).toBeTruthy()
  })
})

describe('session ended', () => {
  it('a 401 from any request sends the user to /login with a message', async () => {
    stubFetch({
      'GET /api/v1/auth/me': employeeUser,
      'GET /api/v1/dashboard': jsonResponse(401, { detail: 'Not authenticated' }),
      'GET /api/v1/work-queue': jsonResponse(401, { detail: 'Not authenticated' }),
    })
    renderApp('/')
    expect(await screen.findByRole('heading', { name: 'Sign in' })).toBeTruthy()
    expect(screen.getByText('Your session has ended. Please sign in again.')).toBeTruthy()
  })
})
