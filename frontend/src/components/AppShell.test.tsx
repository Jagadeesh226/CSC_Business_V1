import { afterEach, describe, expect, it, vi } from 'vitest'
import { screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { emptyDashboard, emptyQueue, employeeUser, renderApp, stubFetch } from '../test/utils'

afterEach(() => vi.unstubAllGlobals())

describe('user menu', () => {
  it('is a disclosure: no menu role claim, Escape closes it and returns focus to the button', async () => {
    stubFetch({ 'GET /api/v1/auth/me': employeeUser, 'GET /api/v1/dashboard': emptyDashboard, 'GET /api/v1/work-queue': emptyQueue })
    renderApp('/')
    const toggle = await screen.findByRole('button', { name: /Ravi Employee/ })
    expect(toggle.getAttribute('aria-haspopup')).toBeNull()
    expect(toggle.getAttribute('aria-expanded')).toBe('false')
    await userEvent.click(toggle)
    expect(toggle.getAttribute('aria-expanded')).toBe('true')
    expect(screen.getByRole('link', { name: 'Change password' })).toBeTruthy()
    await userEvent.tab()
    expect(document.activeElement).toBe(screen.getByRole('link', { name: 'Change password' }))
    await userEvent.keyboard('{Escape}')
    expect(screen.queryByRole('link', { name: 'Change password' })).toBeNull()
    expect(document.activeElement).toBe(toggle)
  })
})
