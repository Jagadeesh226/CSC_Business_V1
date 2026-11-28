import { afterEach, describe, expect, it, vi } from 'vitest'
import { screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { adminUser, emptyDashboard, emptyQueue, jsonResponse, renderApp, stubFetch } from '../test/utils'

afterEach(() => vi.unstubAllGlobals())

describe('login page', () => {
  it('shows validation errors and does not call the server', async () => {
    const fetchMock = stubFetch({ 'GET /api/v1/auth/me': jsonResponse(401, { detail: 'Not authenticated' }) })
    renderApp('/login')
    await userEvent.click(await screen.findByRole('button', { name: 'Sign in' }))
    expect(await screen.findByText('Enter your email address.')).toBeTruthy()
    expect(screen.getByText('Enter your password.')).toBeTruthy()
    expect(screen.getByLabelText('Email').getAttribute('aria-invalid')).toBe('true')
    expect(fetchMock.mock.calls.some(([, init]) => (init as RequestInit)?.method === 'POST')).toBe(false)
  })

  it('submits credentials and lands on the dashboard', async () => {
    const fetchMock = stubFetch({
      'GET /api/v1/auth/me': jsonResponse(401, { detail: 'Not authenticated' }),
      'POST /api/v1/auth/login': jsonResponse(200, { user: adminUser, csrf_token: 'abc' }),
      'GET /api/v1/dashboard': emptyDashboard,
      'GET /api/v1/work-queue': emptyQueue,
    })
    renderApp('/login')
    await userEvent.type(await screen.findByLabelText('Email'), 'asha@example.com')
    await userEvent.type(screen.getByLabelText('Password'), 'a-long-password')
    await userEvent.click(screen.getByRole('button', { name: 'Sign in' }))
    expect(await screen.findByRole('heading', { name: 'Dashboard' })).toBeTruthy()
    const login = fetchMock.mock.calls.find(([, init]) => (init as RequestInit)?.method === 'POST')
    expect(JSON.parse((login![1] as RequestInit).body as string)).toEqual({
      email: 'asha@example.com',
      password: 'a-long-password',
    })
  })

  it('shows the server message when sign-in fails', async () => {
    stubFetch({
      'GET /api/v1/auth/me': jsonResponse(401, { detail: 'Not authenticated' }),
      'POST /api/v1/auth/login': jsonResponse(401, { detail: 'Invalid email or password.' }),
    })
    renderApp('/login')
    await userEvent.type(await screen.findByLabelText('Email'), 'asha@example.com')
    await userEvent.type(screen.getByLabelText('Password'), 'wrong')
    await userEvent.click(screen.getByRole('button', { name: 'Sign in' }))
    await waitFor(() => expect(screen.getByRole('alert').textContent).toContain('Invalid email or password.'))
  })

  it('sends signed-out visitors to /login', async () => {
    stubFetch({ 'GET /api/v1/auth/me': jsonResponse(401, { detail: 'Not authenticated' }) })
    renderApp('/jobs')
    expect(await screen.findByRole('heading', { name: 'Sign in' })).toBeTruthy()
  })
})
