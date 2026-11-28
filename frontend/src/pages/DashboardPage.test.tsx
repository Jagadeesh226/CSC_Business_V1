import { afterEach, describe, expect, it, vi } from 'vitest'
import { screen } from '@testing-library/react'
import { adminUser, emptyDashboard, emptyQueue, renderApp, stubFetch } from '../test/utils'

afterEach(() => vi.unstubAllGlobals())

function tile(label: string): HTMLElement {
  return screen.getByText(label).closest('li') as HTMLElement
}

describe('dashboard', () => {
  it('says whose jobs are shown', async () => {
    stubFetch({
      'GET /api/v1/auth/me': adminUser,
      'GET /api/v1/dashboard': { ...emptyDashboard, scope: 'mine' },
      'GET /api/v1/work-queue': emptyQueue,
    })
    renderApp('/')
    expect(await screen.findByText(/Showing your own jobs\./)).toBeTruthy()
    expect(screen.queryByText(/Showing all jobs\./)).toBeNull()
  })

  it('says all jobs for the whole-centre view and flags overdue work in text', async () => {
    stubFetch({
      'GET /api/v1/auth/me': adminUser,
      'GET /api/v1/dashboard': emptyDashboard, // overdue: 2
      'GET /api/v1/work-queue': emptyQueue,
    })
    renderApp('/')
    expect(await screen.findByText(/Showing all jobs\./)).toBeTruthy()
    expect(tile('Overdue').className).toContain('tile-attention')
    expect(tile('Overdue').textContent).toContain('(needs attention)')
    expect(tile('Due today').textContent).not.toContain('needs attention')
  })

  it('does not flag overdue when there are none', async () => {
    stubFetch({
      'GET /api/v1/auth/me': adminUser,
      'GET /api/v1/dashboard': { ...emptyDashboard, jobs: { ...emptyDashboard.jobs, overdue: 0 } },
      'GET /api/v1/work-queue': emptyQueue,
    })
    renderApp('/')
    await screen.findByText(/Showing all jobs\./)
    expect(tile('Overdue').className).not.toContain('tile-attention')
    expect(tile('Overdue').textContent).not.toContain('needs attention')
  })
})
