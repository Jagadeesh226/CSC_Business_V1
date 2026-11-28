import type { ReactElement } from 'react'
import { render } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { vi } from 'vitest'
import { AppRoutes } from '../routes'
import { setApiHandlers } from '../lib/api'

export function jsonResponse(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  })
}

type Handler = (url: URL, init: RequestInit) => Response | Promise<Response>

/** Replace fetch with a handler keyed on "METHOD /path". Unknown requests fail the test. */
export function stubFetch(routes: Record<string, Handler | Response | object>) {
  const fn = vi.fn(async (input: RequestInfo | URL, init: RequestInit = {}) => {
    const url = new URL(String(input), 'http://localhost')
    const key = `${(init.method ?? 'GET').toUpperCase()} ${url.pathname}`
    const route = routes[key]
    if (route === undefined) throw new Error(`Unexpected request: ${key}`)
    if (typeof route === 'function') return route(url, init)
    if (route instanceof Response) return route.clone()
    return jsonResponse(200, route)
  })
  vi.stubGlobal('fetch', fn)
  return fn
}

export const adminUser = {
  id: 1,
  name: 'Asha Admin',
  email: 'asha@example.com',
  role: 'admin' as const,
  is_active: true,
  must_change_password: false,
  created_at: '2026-11-01T00:00:00Z',
}
export const employeeUser = { ...adminUser, id: 2, name: 'Ravi Employee', email: 'ravi@example.com', role: 'employee' as const }

export function renderWithProviders(ui: ReactElement, path = '/') {
  setApiHandlers({})
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const result = render(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={[path]}>{ui}</MemoryRouter>
    </QueryClientProvider>,
  )
  return { ...result, client }
}

export function renderApp(path = '/') {
  return renderWithProviders(<AppRoutes />, path)
}

export const emptyDashboard = {
  as_of: '2026-11-28',
  scope: 'all',
  jobs: {
    open: { pending: 1, in_progress: 2, waiting_for_documents: 3, submitted: 4 },
    due_today: 1,
    overdue: 2,
    created_today: 0,
    completed_today: 0,
    created_this_month: 0,
    completed_this_month: 0,
    cancelled_this_month: 0,
  },
  recent_jobs: [],
  unassigned_open_jobs: null,
  workload: null,
}
export const emptyQueue = {
  as_of: '2026-11-28',
  documents_to_review: 5,
  jobs_missing_documents: 0,
  jobs_processing: 0,
  jobs_ready_to_submit: 0,
  jobs_ready_to_resume: 0,
  jobs_overdue: 0,
  jobs_due_today: 0,
  vehicle_readings_to_check: 0,
  reminders_to_send: 0,
  lookups_waiting_for_captcha: 0,
  unread_notifications: 0,
  jobs_with_balance_due: 0,
  unassigned_jobs: null,
  jobs_overpaid: null,
}
